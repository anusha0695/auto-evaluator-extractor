"""Preprocessing view — PDF rendered with color-coded DocAI block overlays."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from ui.phase1.components.pdf_viewer import color_for_role, render_page_with_blocks
from ui.phase1.data_layer import RunSummary, load_artifact


# Common locations the user's local PDFs live in.
_PDF_SEARCH_PATHS = [
    Path(__file__).resolve().parents[3] / ".." / "data" / "actual_docs",
    Path(__file__).resolve().parents[3] / "data" / "actual_docs",
    Path(__file__).resolve().parents[3] / "data" / "input" / "raw_documents",
]


def _find_pdf(doc_id: str, user_override: str | None) -> Path | None:
    if user_override:
        p = Path(user_override).expanduser().resolve()
        return p if p.exists() else None
    for dir_ in _PDF_SEARCH_PATHS:
        candidate = (dir_ / f"{doc_id}.pdf").resolve()
        if candidate.exists():
            return candidate
    return None


def render(*, run: RunSummary) -> None:
    st.header(f"📐 Preprocessing — {run.doc_id}")
    st.caption(
        "DocAI Layout Parser blocks overlaid on the rendered PDF. Each color "
        "is one `text_role` from the Block Profiler's closed vocabulary."
    )

    # ----- PDF source -----------------------------------------------------
    auto_pdf = _find_pdf(run.doc_id, None)
    pdf_path_str = st.text_input(
        "Local PDF path",
        value=str(auto_pdf) if auto_pdf else "",
        help=(
            "Path to the local PDF. The UI auto-searches "
            "../data/actual_docs/, data/actual_docs/, data/input/raw_documents/. "
            "Override here if your file lives elsewhere."
        ),
    )
    pdf_path = _find_pdf(run.doc_id, pdf_path_str)
    if pdf_path is None:
        st.error(
            f"PDF not found. Searched: `{pdf_path_str}` plus the default locations. "
            f"Paste a valid local path above."
        )
        return

    # ----- DocAI artifacts ------------------------------------------------
    docai_raw = load_artifact(run.doc_id, "docai_raw")
    block_profiles = load_artifact(run.doc_id, "block_profiles") or []

    if docai_raw is None:
        st.warning(
            "No `docai_raw.json` artifact found for this run. Block overlays "
            "won't be drawn; the PDF will render without them."
        )
        blocks_by_id: dict[str, dict[str, Any]] = {}
    else:
        # Walk the cached document_layout.blocks to get bbox + page metadata.
        blocks_by_id = _flatten_layout_blocks(docai_raw)

    profiles_by_id = {p.get("block_id"): p for p in block_profiles}

    # Merge bbox (from docai_raw) + text_role (from block_profiles) per block.
    merged: list[dict[str, Any]] = []
    for bid, b in blocks_by_id.items():
        prof = profiles_by_id.get(bid, {})
        merged.append({
            "block_id": bid,
            "page_number": b.get("page_number"),
            "bbox": b.get("bbox"),
            "text_role": prof.get("text_role", "other"),
            "target_umbrella_hint": ", ".join(
                prof.get("target_umbrella_hints")
                or ([prof["target_umbrella_hint"]] if prof.get("target_umbrella_hint") else [])
            ) or None,
            "confidence": prof.get("confidence"),
        })

    if not merged:
        st.info("No block info available — rendering raw PDF pages.")

    # ----- Legend ---------------------------------------------------------
    roles_seen = sorted({b["text_role"] for b in merged})
    if roles_seen:
        with st.expander("Legend — text_role colors", expanded=False):
            for role in roles_seen:
                count = sum(1 for b in merged if b["text_role"] == role)
                col = color_for_role(role)
                st.markdown(
                    f'<span style="display:inline-block;width:14px;height:14px;'
                    f'background:{col};margin-right:6px;border-radius:2px;'
                    f'vertical-align:middle"></span> '
                    f'`{role}` — {count} block(s)',
                    unsafe_allow_html=True,
                )

    # ----- Pages ----------------------------------------------------------
    n_pages = max((b["page_number"] or 1 for b in merged), default=1)
    page_num = st.slider("Page", 1, n_pages, 1) if n_pages > 1 else 1

    page_blocks = [b for b in merged if (b["page_number"] or 1) == page_num]
    png = render_page_with_blocks(
        pdf_path=pdf_path,
        page_number=page_num,
        blocks_with_profiles=page_blocks,
    )
    if png is None:
        st.error("PDF render failed. Is poppler installed? `brew install poppler`.")
        return
    st.image(png, caption=f"Page {page_num} of {n_pages}  ·  {len(page_blocks)} block(s)")

    # ----- Block list -----------------------------------------------------
    with st.expander(f"Block details ({len(page_blocks)} on this page)", expanded=False):
        for b in page_blocks:
            col = color_for_role(b["text_role"])
            confidence = b.get("confidence")
            conf_str = f" conf={confidence:.2f}" if confidence is not None else ""
            st.markdown(
                f'<div style="border-left:4px solid {col};padding-left:8px;margin:4px 0">'
                f"<code>{b['block_id']}</code> · "
                f"<b>{b['text_role']}</b>{conf_str} → "
                f"<i>{b.get('target_umbrella_hint','—')}</i>"
                f"</div>",
                unsafe_allow_html=True,
            )


def _flatten_layout_blocks(docai_raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Walk the cached DocAI Document JSON and emit {block_id: {bbox, page_number}}."""
    out: dict[str, dict[str, Any]] = {}
    layout = docai_raw.get("documentLayout") or docai_raw.get("document_layout")
    if not layout:
        return out
    _walk(layout.get("blocks") or [], out, page_number=1)
    return out


def _walk(blocks: list[Any], out: dict[str, dict[str, Any]], *, page_number: int) -> None:
    for block in blocks:
        bid = str(block.get("blockId") or block.get("block_id") or "")
        page_span = block.get("pageSpan") or block.get("page_span") or {}
        pnum = int(page_span.get("pageStart") or page_span.get("page_start") or page_number)

        text_block = block.get("textBlock") or block.get("text_block")
        nested = []
        bbox: list[float] = []
        if text_block:
            layout = text_block.get("layout") or {}
            bp = layout.get("boundingPoly") or layout.get("bounding_poly") or {}
            verts = bp.get("normalizedVertices") or bp.get("normalized_vertices") or bp.get("vertices") or []
            if verts:
                xs = [float(v.get("x") or 0) for v in verts]
                ys = [float(v.get("y") or 0) for v in verts]
                bbox = [min(xs), min(ys), max(xs), max(ys)]
            nested = text_block.get("blocks") or []
        if bid:
            out[bid] = {"bbox": bbox, "page_number": pnum}
        if nested:
            _walk(nested, out, page_number=pnum)
