"""
DocAIParser — wraps the DocAI Layout Parser processor.

One method that matters:

    DocAIParser.parse(doc_id, gcs_uri) -> (DocProfile, raw_response_uri)

Returns:
    DocProfile:  total_pages + per-page text (PageText[]) + flat block list
                 (BlockInfo[]) + section_path strings for downstream Block
                 Profiler tagging.
    raw_response_uri:  gs:// path where the raw DocAI response is cached.

Processor pin (from .env / config):
    pretrained-layout-parser-v1.6-2026-01-13   id=81e83f6783d90bb0   location=us

The DocAI client is sync; we wrap calls in asyncio.to_thread() so the
LangGraph event loop isn't blocked. Tenacity retries with exponential backoff
on transient errors (HTTP 5xx, ResourceExhausted).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.errors import DocAIError
from core.gcs_client import GcsUri
from core.observability import trace
from core.persistence import Persistence
from core.state import BlockInfo, BlockSpan, DocProfile, PageText

logger = logging.getLogger(__name__)


def concat_blocks_with_spans(
    blocks_on_page: list[BlockInfo],
    *,
    exclude_block_ids: set[str] | None = None,
) -> tuple[str, list[BlockSpan]]:
    """Concatenate the given blocks' text (newline-joined) and record each
    block's [start, end) char span within the result.

    Shared by DocAIParser (initial page text) and FaxHeaderFilter (rebuild
    after dropping fax-noise blocks) so the spans always stay valid against
    the exact text SciSpaCy will run on. `exclude_block_ids` skips blocks
    (e.g. fax-noise) without disturbing the offsets of the survivors.
    """
    exclude = exclude_block_ids or set()
    parts: list[str] = []
    spans: list[BlockSpan] = []
    cursor = 0
    for b in blocks_on_page:
        if b.get("block_id") in exclude:
            continue
        btext = (b.get("text") or "").strip()
        if not btext:
            continue
        if parts:
            cursor += 1  # the "\n" separator join() will insert
        start = cursor
        end = start + len(btext)
        spans.append(BlockSpan(block_id=b["block_id"], start=start, end=end))
        parts.append(btext)
        cursor = end
    return "\n".join(parts), spans


class DocAIParser:
    """Wraps DocAI Layout Parser. Caches raw response to GCS on every parse."""

    def __init__(
        self,
        *,
        persistence: Persistence,
        project_id: str | None = None,
        location: str | None = None,
        processor_id: str | None = None,
        processor_version: str | None = None,
    ) -> None:
        self._persistence = persistence
        self._project_id = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self._location = location or os.environ.get("DOCAI_LOCATION", "us")
        self._processor_id = processor_id or os.environ.get("DOCAI_PROCESSOR_ID", "")
        self._processor_version = processor_version or os.environ.get(
            "DOCAI_PROCESSOR_VERSION", ""
        )
        if not (self._project_id and self._processor_id):
            raise DocAIError(
                "DocAIParser requires GCP_PROJECT_ID and DOCAI_PROCESSOR_ID "
                "(unset). Edit .env from .env.example.",
                retry_safe=False,
            )

        self._client: Any = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = await asyncio.to_thread(self._make_client)
        return self._client

    def _make_client(self) -> Any:
        from google.cloud import documentai_v1 as documentai

        api_endpoint = f"{self._location}-documentai.googleapis.com"
        return documentai.DocumentProcessorServiceClient(
            client_options={"api_endpoint": api_endpoint}
        )

    @property
    def _processor_name(self) -> str:
        from google.cloud import documentai_v1 as documentai

        client = documentai.DocumentProcessorServiceClient
        if self._processor_version:
            return client.processor_version_path(
                self._project_id,
                self._location,
                self._processor_id,
                self._processor_version,
            )
        return client.processor_path(self._project_id, self._location, self._processor_id)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    @trace("preprocess.docai.parse")
    async def parse(
        self,
        doc_id: str,
        gcs_uri: str | None = None,
        *,
        raw_pdf_bytes: bytes | None = None,
    ) -> tuple[DocProfile, str]:
        """Parse a PDF. Returns (DocProfile, raw_response_uri).

        Exactly ONE input source must be set:
          - `gcs_uri`        — DocAI fetches the PDF from GCS itself.
          - `raw_pdf_bytes`  — local file bytes; sent inline to DocAI.
                                Useful for fast dev iteration without
                                uploading to GCS first. Output artifacts
                                still go to GCS regardless.
        """
        if (gcs_uri is None) == (raw_pdf_bytes is None):
            raise DocAIError(
                "DocAIParser.parse: provide EXACTLY one of gcs_uri / raw_pdf_bytes",
                doc_id=doc_id,
                retry_safe=False,
            )
        if gcs_uri is not None and not gcs_uri.lower().endswith(".pdf"):
            raise DocAIError(
                f"DocAIParser only accepts PDFs (got {gcs_uri!r})",
                doc_id=doc_id,
                retry_safe=False,
            )

        from google.cloud import documentai_v1 as documentai

        client = await self._get_client()
        source_label = gcs_uri or f"<inline {len(raw_pdf_bytes or b'')}-byte PDF>"

        # IMPORTANT: Layout Parser only returns bounding_box coordinates when
        # process_options.layout_config.return_bounding_boxes is explicitly
        # enabled. Without this, every Block comes back spatially blank and
        # the UI overlay layer has nothing to draw. Enable always.
        process_options = documentai.ProcessOptions(
            layout_config=documentai.ProcessOptions.LayoutConfig(
                return_bounding_boxes=True,
            ),
        )

        if gcs_uri is not None:
            gcs_doc = documentai.GcsDocument(
                gcs_uri=gcs_uri, mime_type="application/pdf",
            )
            request = documentai.ProcessRequest(
                name=self._processor_name,
                gcs_document=gcs_doc,
                process_options=process_options,
            )
        else:
            raw_doc = documentai.RawDocument(
                content=raw_pdf_bytes, mime_type="application/pdf",
            )
            request = documentai.ProcessRequest(
                name=self._processor_name,
                raw_document=raw_doc,
                process_options=process_options,
            )

        def _do_process() -> Any:
            return client.process_document(request=request)

        try:
            response = await asyncio.to_thread(_do_process)
        except Exception as exc:
            raise DocAIError(
                f"DocAI process_document failed for {source_label}: {exc}",
                doc_id=doc_id,
                retry_safe=True,
                context={"source": source_label, "error": str(exc)},
            ) from exc

        # ----- Cache raw response to GCS for replay / audit ----------------
        # Proto-plus's to_json signature has churned across SDK versions
        # (e.g. `including_default_value_fields` was removed). The safest
        # path is to go via the dict representation, then json.dumps. This
        # guarantees the artifact file is always parseable by `json.loads`
        # on the read side.
        raw_dict = self._document_to_jsonable_dict(response.document)
        import json as _json
        raw_json = _json.dumps(raw_dict, indent=2, default=str)
        # Defensive: confirm the string round-trips before persisting.
        _json.loads(raw_json)

        # ----- Diagnostic: did the server actually return bboxes? ----------
        # Walks the proto tree directly (bypassing to_dict()) so we know
        # whether bboxes are missing because the server didn't send them
        # vs. proto-plus stripping them during serialization.
        bbox_proto_count, layout_blocks_count = self._count_proto_bboxes(
            response.document
        )
        logger.info(
            "DocAI bbox diagnostic: %d blocks have bounding_box set on the "
            "wire (out of %d total). If 0/N, the processor version is "
            "ignoring process_options.layout_config.return_bounding_boxes.",
            bbox_proto_count, layout_blocks_count,
        )

        # ----- Full-fidelity proto dump (artifact: docai_raw_full) ---------
        # `to_dict()` strips empty / default-valued submessages. To see
        # exactly what the server sent on the wire (including empty
        # bounding_box stubs, empty pages, etc.), we also serialize via
        # google.protobuf.json_format.MessageToJson with default-field
        # inclusion enabled. This is the artifact to inspect when the
        # standard docai_raw.json looks suspicious.
        full_json = self._document_to_full_json(response.document)
        await self._persistence.write_artifact(
            doc_id=doc_id, kind="docai_raw_full", content=full_json,
        )

        # ----- Raw proto bytes dump (artifact: docai_raw_proto.pb) ---------
        # If even MessageToJson omits something, this is the bytes the
        # server literally sent. Inspect with:
        #   python -c "from google.cloud import documentai_v1 as d; \
        #              doc = d.Document.deserialize(open('docai_raw_proto.pb','rb').read()); \
        #              print(doc)"
        try:
            proto_bytes = type(response.document).serialize(response.document)
            await self._persistence.write_artifact(
                doc_id=doc_id,
                kind="docai_raw_proto",
                content=proto_bytes,
                ext="pb",
            )
        except Exception as exc:
            logger.warning(
                "Could not write docai_raw_proto bytes dump: %s", exc,
            )

        # ----- Proto-text repr dump (artifact: docai_raw_text.txt) ---------
        # Human-readable proto text. Lets you grep the response with
        # `grep bounding_box docai_raw_text.txt` to spot bboxes in-place
        # without any JSON-serializer in the middle.
        try:
            proto_text = str(response.document)
            await self._persistence.write_artifact(
                doc_id=doc_id,
                kind="docai_raw_text",
                content=proto_text,
                ext="txt",
            )
        except Exception as exc:
            logger.warning(
                "Could not write docai_raw_text dump: %s", exc,
            )

        raw_uri = await self._persistence.write_artifact(
            doc_id=doc_id, kind="docai_raw", content=raw_json
        )

        # ----- Convert proto → our DocProfile shape ------------------------
        doc_profile = self._document_to_doc_profile(response.document, raw_uri)
        logger.info(
            "DocAIParser.parse(doc_id=%s, source=%s) → %d pages, %d blocks, raw=%s",
            doc_id,
            source_label,
            doc_profile.get("total_pages"),
            len(doc_profile.get("blocks") or []),
            raw_uri,
        )
        return doc_profile, raw_uri

    # -----------------------------------------------------------------------
    # Document → DocProfile mapping
    # -----------------------------------------------------------------------

    def _document_to_doc_profile(self, document: Any, raw_uri: str) -> DocProfile:
        """Walk the DocAI Document proto and produce a DocProfile."""
        # ----- Blocks: walk document_layout.blocks (Layout Parser v1.6 schema)
        # if available; otherwise fall back to per-page paragraph blocks
        # (older processor versions). -----
        blocks: list[BlockInfo] = []
        layout = getattr(document, "document_layout", None)
        if layout and getattr(layout, "blocks", None):
            blocks = list(self._walk_layout_blocks(layout.blocks, parent_path=""))
        else:
            blocks = self._fallback_blocks_from_pages(document)

        # ----- Pages -----
        # Layout Parser doesn't always populate `document.pages` — when it
        # doesn't, synthesize one PageText per unique page_number from the
        # flat block list. Per-page text becomes the concatenation of all
        # text blocks on that page in input order.
        pages: list[PageText] = []
        per_page_block_ids: dict[int, list[str]] = {}

        if list(getattr(document, "pages", []) or []):
            # Legacy path: per-page metadata IS present in document.pages.
            # (Not used by Layout Parser v1.6+ — document.pages is empty there.)
            # block_spans left empty: the deterministic NER mapper falls back
            # to page-level attribution if spans are absent.
            for page in document.pages:
                page_number = int(page.page_number) if page.page_number else 1
                page_text = self._extract_page_text(document.text or "", page)
                pages.append(PageText(
                    page_number=page_number, text=page_text,
                    block_ids_on_page=[], block_spans=[],
                ))
                per_page_block_ids[page_number] = []
        else:
            # New path (Layout Parser v1.6+): synthesize pages from blocks.
            # 2C: build the concatenated page text AND record each block's
            # [start, end) char span via the shared helper, so the Medical
            # NER stage can map an entity's offset back to its source block.
            seen_page_numbers: list[int] = []
            for b in blocks:
                pnum = b.get("page_number") or 1
                if pnum not in seen_page_numbers:
                    seen_page_numbers.append(pnum)
            for pnum in sorted(seen_page_numbers):
                blocks_on_page = [
                    b for b in blocks if (b.get("page_number") or 1) == pnum
                ]
                page_text, spans = concat_blocks_with_spans(blocks_on_page)
                pages.append(PageText(
                    page_number=pnum, text=page_text,
                    block_ids_on_page=[], block_spans=spans,
                ))

        # Bind block_id → page mapping into each PageText (may already be
        # populated for the synthesized path; redo idempotently for both).
        per_page_block_ids = {}
        for b in blocks:
            pnum = b.get("page_number") or 1
            per_page_block_ids.setdefault(pnum, []).append(b["block_id"])
        for page in pages:
            page["block_ids_on_page"] = per_page_block_ids.get(page["page_number"], [])

        return DocProfile(
            total_pages=len(pages),
            pages=pages,
            blocks=blocks,
            block_profiles=[],            # filled later by BlockProfiler
            raw_docai_gcs_uri=raw_uri,
            fax_header_blocks_removed=0,  # filled later by FaxHeaderFilter
        )

    @staticmethod
    def _count_proto_bboxes(document: Any) -> tuple[int, int]:
        """Walk the raw proto tree counting how many DocumentLayoutBlocks
        carry a populated bounding_box. This bypasses to_dict() so we know
        whether bboxes are missing because the server didn't send them
        vs. the serializer dropping them."""
        layout = getattr(document, "document_layout", None)
        if not layout or not getattr(layout, "blocks", None):
            return 0, 0

        with_bbox = 0
        total = 0

        def _walk(blocks: Any) -> None:
            nonlocal with_bbox, total
            for b in blocks:
                total += 1
                bp = getattr(b, "bounding_box", None)
                if bp:
                    verts = list(getattr(bp, "normalized_vertices", []) or []) \
                        or list(getattr(bp, "vertices", []) or [])
                    if verts:
                        with_bbox += 1
                tb = getattr(b, "text_block", None)
                if tb and getattr(tb, "blocks", None):
                    _walk(tb.blocks)
                tab = getattr(b, "table_block", None)
                if tab:
                    rows = list(getattr(tab, "header_rows", []) or []) \
                        + list(getattr(tab, "body_rows", []) or [])
                    for r in rows:
                        for cell in (r.cells or []):
                            _walk(cell.blocks or [])

        _walk(layout.blocks)
        return with_bbox, total

    @staticmethod
    def _document_to_full_json(document: Any) -> str:
        """Full-fidelity JSON dump that INCLUDES default-valued fields.

        proto-plus `to_dict()` skips empty submessages — so an unpopulated
        `bounding_box` simply vanishes from the artifact, making it
        impossible to tell whether the server omitted it or our serializer
        dropped it. This helper goes through google.protobuf.json_format
        with `including_default_value_fields=True` (proto API name varies
        slightly across versions) to keep every field present.

        Output is also indented for human inspection. JSON-validates before
        returning.
        """
        import json as _json

        try:
            from google.protobuf.json_format import MessageToJson
            pb = document._pb if hasattr(document, "_pb") else document

            # google.protobuf renamed the kwarg in 5.x.
            # Try the new name first, then the old.
            try:
                full = MessageToJson(
                    pb,
                    indent=2,
                    preserving_proto_field_name=True,
                    always_print_fields_with_no_presence=True,
                )
            except TypeError:
                full = MessageToJson(
                    pb,
                    indent=2,
                    preserving_proto_field_name=True,
                    including_default_value_fields=True,
                )
            _json.loads(full)  # round-trip check
            return full
        except Exception as exc:
            logger.warning(
                "Could not produce full-fidelity DocAI JSON dump (%s) — "
                "falling back to standard to_dict() output.", exc,
            )
            return _json.dumps(
                DocAIParser._document_to_jsonable_dict(document),
                indent=2, default=str,
            )

    @staticmethod
    def _document_to_jsonable_dict(document: Any) -> dict[str, Any]:
        """Convert a DocAI Document proto-plus message to a JSON-serializable dict.

        Tries multiple SDK-version-tolerant paths:
          1. proto-plus `type(document).to_dict(document)` — returns a plain dict tree.
          2. `type(document).to_json(document)` then `json.loads()` — older proto-plus.
          3. google.protobuf.json_format.MessageToDict on the inner `_pb` instance.
          4. Stub dict with proto-text repr if all else fails (so the artifact
             write doesn't crash the run).
        """
        import json as _json

        try:
            return type(document).to_dict(document)
        except Exception:
            pass

        try:
            return _json.loads(type(document).to_json(document))
        except Exception:
            pass

        try:
            from google.protobuf.json_format import MessageToDict
            return MessageToDict(document._pb if hasattr(document, "_pb") else document)
        except Exception as exc:
            logger.warning(
                "DocAIParser: could not serialize Document to dict (%s) — "
                "writing a stub artifact so downstream code doesn't crash. "
                "The proto-text representation is preserved in `_raw_repr`.",
                exc,
            )
            return {
                "_serialization_failed": True,
                "_raw_repr": str(document)[:50_000],
            }

    @staticmethod
    def _extract_page_text(full_text: str, page: Any) -> str:
        """Return the page's text by slicing the full document text via its
        page-anchor text segments."""
        if not full_text or not page.layout or not page.layout.text_anchor:
            return ""
        segs = page.layout.text_anchor.text_segments or []
        if not segs:
            return ""
        return "".join(
            full_text[int(s.start_index or 0): int(s.end_index or 0)] for s in segs
        )

    def _walk_layout_blocks(
        self,
        layout_blocks: Any,
        *,
        parent_path: str,
    ) -> Any:
        """Recursively walk DocAI document_layout.blocks tree, yielding flat
        BlockInfo records for each leaf text block. section_path encodes the
        ancestry (e.g. 'page_1/heading/paragraph')."""
        for block in layout_blocks:
            block_id = str(getattr(block, "block_id", "") or "")
            page_span = getattr(block, "page_span", None)
            page_number = int(getattr(page_span, "page_start", 1)) if page_span else 1

            text_block = getattr(block, "text_block", None)
            table_block = getattr(block, "table_block", None)
            list_block = getattr(block, "list_block", None)
            image_block = getattr(block, "image_block", None)

            block_type = "unknown"
            if text_block:
                block_type = (getattr(text_block, "type_", None) or
                              getattr(text_block, "type", None) or "text")
            elif table_block:
                block_type = "table"
            elif list_block:
                block_type = "list"
            elif image_block:
                block_type = "image"

            section_path = f"{parent_path}/{block_type}" if parent_path else f"page_{page_number}/{block_type}"

            # If this is a leaf text block, emit it.
            text = ""
            if text_block and getattr(text_block, "text", ""):
                text = str(text_block.text)
            bbox = self._extract_bbox(block)

            if text or bbox:
                yield BlockInfo(
                    block_id=block_id,
                    page_number=page_number,
                    bbox=bbox or [],
                    section_path=section_path,
                    text=text,
                )

            # Recurse into nested children if present
            nested = []
            if text_block and getattr(text_block, "blocks", None):
                nested = text_block.blocks
            elif table_block and getattr(table_block, "body_rows", None):
                # Tables: walk each cell's blocks
                nested = [
                    cell_block
                    for row in (
                        list(getattr(table_block, "header_rows", []) or [])
                        + list(getattr(table_block, "body_rows", []) or [])
                    )
                    for cell in (row.cells or [])
                    for cell_block in (cell.blocks or [])
                ]
            if nested:
                yield from self._walk_layout_blocks(nested, parent_path=section_path)

    @staticmethod
    def _extract_bbox(block: Any) -> list[float]:
        """Return [x0, y0, x1, y1] from the block's bounding poly.

        DocAI Layout Parser puts the bounding poly directly on the
        DocumentLayoutBlock (`block.bounding_box`), NOT under
        `text_block.layout.bounding_poly`. It is only populated when the
        ProcessRequest enabled `process_options.layout_config
        .return_bounding_boxes = True` (see `parse()` above).

        Falls back to [] if the block has no spatial info.
        """
        poly = getattr(block, "bounding_box", None)
        if not poly:
            return []
        verts = (
            list(getattr(poly, "normalized_vertices", []) or [])
            or list(getattr(poly, "vertices", []) or [])
        )
        if not verts:
            return []
        xs = [float(getattr(v, "x", 0) or 0) for v in verts]
        ys = [float(getattr(v, "y", 0) or 0) for v in verts]
        return [min(xs), min(ys), max(xs), max(ys)]

    def _fallback_blocks_from_pages(self, document: Any) -> list[BlockInfo]:
        """Older DocAI processors don't emit document_layout. Fall back to
        per-page paragraph blocks so the downstream pipeline still works."""
        out: list[BlockInfo] = []
        for page in document.pages:
            page_number = int(page.page_number) if page.page_number else 1
            for i, para in enumerate(page.paragraphs or []):
                text = self._extract_page_text(document.text or "", para)
                bbox = self._extract_bbox_from_layout(para.layout)
                out.append(
                    BlockInfo(
                        block_id=f"p{page_number}-para-{i}",
                        page_number=page_number,
                        bbox=bbox,
                        section_path=f"page_{page_number}/paragraph",
                        text=text,
                    )
                )
        return out

    @staticmethod
    def _extract_bbox_from_layout(layout: Any) -> list[float]:
        if not layout or not getattr(layout, "bounding_poly", None):
            return []
        poly = layout.bounding_poly
        verts = getattr(poly, "normalized_vertices", []) or getattr(poly, "vertices", [])
        if not verts:
            return []
        xs = [float(v.x or 0) for v in verts]
        ys = [float(v.y or 0) for v in verts]
        return [min(xs), min(ys), max(xs), max(ys)]
