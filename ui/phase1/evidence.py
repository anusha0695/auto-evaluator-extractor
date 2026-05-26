"""
Shared evidence rendering for the SME tabs (P3-M8d / entity browser).

`build_evidence_html` renders the actual page image and lays a HIGHLIGHTER over
the entity — a translucent fill with `mix-blend-mode: multiply` so the text reads
THROUGH it (no border, no box covering the text, font color untouched). The
highlight region comes from `word_boxes_for_entity` (OCR tokens if present, else
the no-OCR character interpolation, else the block box). A synced text strip shows
the block text with the exact entity surface `<mark>`-highlighted (background only).

`enumerate_entities` walks an extraction envelope and returns every record that
carries provenance (`occurrences`), so the Entity browser can offer ALL entities —
not just the flagged ones — through the same jump+highlight+trace experience.
"""

from __future__ import annotations

import html
import re
from typing import Any

from preprocess.word_geometry import word_boxes_for_entity
from ui.phase1.field_trace import assemble_field_trace

ENTITY_MARK = "rgba(255, 224, 120, 0.55)"   # highlighter yellow
LINK_MARK = "rgba(120, 180, 255, 0.50)"     # highlighter blue (linked entity)
_TOKEN_RE = re.compile(r"[A-Za-z_]+\[\d+\]")
_INDEX_RE = re.compile(r"\[\d+\]")


def _record_root(ref: str) -> str | None:
    """The top-level RECORD a field belongs to = up to and including the FIRST array
    index, e.g. both '…other_molecular_biomarkers[0].biomarker_name' and
    '…other_molecular_biomarkers[0].findings[1].result' → '…other_molecular_biomarkers[0]';
    'significant_findings.specimen_findings[0].pTNM_staging_details.pTNM_stage' →
    'significant_findings.specimen_findings[0]'. None for flat sections (report_metadata)
    so their fields are NOT mass-linked to each other."""
    m = _INDEX_RE.search(ref or "")
    return ref[: m.end()] if m else None


def _scale(b: list[float]) -> str:
    return (f'left:{b[0]*100:.2f}%;top:{b[1]*100:.2f}%;'
            f'width:{(b[2]-b[0])*100:.2f}%;height:{(b[3]-b[1])*100:.2f}%')


def _marker(b: list[float], fill: str) -> str:
    """A highlighter swipe — translucent fill, multiply blend, NO border."""
    return (f'<div style="position:absolute;{_scale(b)};background:{fill};'
            f'mix-blend-mode:multiply;border-radius:1px;"></div>')


def mark_surface(text: str, surface: str) -> str:
    """Block text with the entity surface background-highlighted (font color
    untouched). Always exact — character match, no geometry needed."""
    safe = html.escape(text or "")
    s = (surface or "").strip()
    if not s:
        return safe
    m = re.search(re.escape(html.escape(s)), safe, flags=re.IGNORECASE)
    if not m:
        return safe
    a, b = m.start(), m.end()
    return (safe[:a]
            + f'<mark style="background:{ENTITY_MARK};color:inherit;padding:0 1px">'
            + safe[a:b] + "</mark>" + safe[b:])


def build_evidence_html(
    *,
    cited_block_ids: list[str],
    surface: str,
    views: dict[str, Any],
    word_geometry: list[dict[str, Any]],
    pages_by_num: dict[int, dict[str, Any]],
) -> str:
    if not cited_block_ids:
        return ('<div style="font:13px sans-serif;color:#6b7280;padding:12px">'
                'No source block recorded for this entity.</div>')
    primary = cited_block_ids[0]
    bv = views.get(primary)
    if bv is None or not bv.bbox:
        return ('<div style="font:13px sans-serif;color:#6b7280;padding:12px">'
                f'Source block <code>{html.escape(str(primary))}</code> has no geometry.</div>')

    page = bv.page_number
    pg = pages_by_num.get(page)
    overlays = []
    for hb in word_boxes_for_entity(word_geometry, page=page, block_bbox=bv.bbox,
                                    surface=surface, block_text=bv.text):
        overlays.append(_marker(hb, ENTITY_MARK))
    for bid in cited_block_ids[1:]:
        ev = views.get(bid)
        if ev and ev.bbox and ev.page_number == page:
            for hb in word_boxes_for_entity(word_geometry, page=page, block_bbox=ev.bbox,
                                            surface=surface, block_text=ev.text):
                overlays.append(_marker(hb, LINK_MARK))

    if pg:
        img = (f'<div style="position:relative;width:100%">'
               f'<img src="{pg["data_uri"]}" style="width:100%;display:block"/>'
               f'{"".join(overlays)}</div>')
    else:
        img = (f'<div style="position:relative;width:100%;padding-top:128%;'
               f'background:#fafafa;border:1px dashed #d1d5db">{"".join(overlays)}</div>')

    strip = (f'<div style="font:12px ui-monospace,monospace;line-height:1.7;'
             f'border:0.5px solid #e5e7eb;border-radius:6px;padding:8px 10px;margin-top:8px">'
             f'{mark_surface(bv.text, surface)}</div>')
    legend = (f'<div style="font:11px sans-serif;color:#6b7280;margin:6px 0">'
              f'page {page} · block {html.escape(str(primary))} · '
              f'<span style="background:{ENTITY_MARK};padding:0 6px">entity</span>'
              + (f' <span style="background:{LINK_MARK};padding:0 6px;margin-left:4px">linked</span>'
                 if len(cited_block_ids) > 1 else "") + '</div>')
    return f'<div style="font-family:sans-serif">{legend}{img}{strip}</div>'


def render_trace(st, trace: list[dict[str, Any]], *, technical: bool, show_binding: bool = False) -> None:
    if not trace:
        st.caption("No recorded processing steps for this field.")
        return
    icon = {"extraction": "📄", "linking": "🔗", "verification": "🔎",
            "repair": "🔧", "resolution": "🤝"}
    shown = [s for s in trace if show_binding or s.get("kind") != "binding"]
    n_binding = sum(1 for s in trace if s.get("kind") == "binding")
    if n_binding and not show_binding:
        st.caption(f"{n_binding} binding-verifier check(s) hidden — toggle “Show binding checks” to see their evidence.")
    for s in shown:
        line = s["technical"] if technical else s["plain"]
        md = (f"{icon.get(s['phase'], '•')} **{s['phase']}** · {html.escape(s['node'])}  \n"
              f"<span style='color:#4b5563'>{html.escape(line)}</span>")
        reasoning = (s.get("reasoning") or "").strip()
        if reasoning:
            lbl = ("note (section-level — not specific to this field)"
                   if s.get("reasoning_scope") == "section" else "why (this field)")
            md += (f"  \n<span style='color:#6b7280;font-style:italic'>"
                   f"↳ {lbl}: {html.escape(reasoning)}</span>")
        st.markdown(md, unsafe_allow_html=True)


def iter_provenance(prov: Any):
    """Yield (field_name, entry) over a provenance value in EITHER shape:
    the new ARRAY [{field_name, block_id, page, type, rationale}, …] or the legacy
    field_name→entry MAP. Tolerating both keeps existing artifacts rendering."""
    if isinstance(prov, dict):
        for k, meta in prov.items():
            if isinstance(meta, dict):
                yield str(k), meta
    elif isinstance(prov, list):
        for meta in prov:
            if isinstance(meta, dict) and meta.get("field_name"):
                yield str(meta["field_name"]), meta


def field_rationale_map(extraction: dict[str, Any]) -> dict[str, str]:
    """Map field ref → its own stored extraction rationale (from any `provenance`
    array/map, e.g. report_metadata). Used for the per-field extraction 'why'."""
    out: dict[str, str] = {}

    def walk(obj: Any, ref: str) -> None:
        if isinstance(obj, dict):
            for fname, meta in iter_provenance(obj.get("provenance")):
                if meta.get("rationale"):
                    out[f"{ref}.{fname}"] = str(meta["rationale"])
            for k, v in obj.items():
                if k != "provenance" and isinstance(v, (dict, list)):
                    walk(v, f"{ref}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, (dict, list)):
                    walk(v, f"{ref}[{i}]")

    for sec, payload in (extraction or {}).items():
        if isinstance(payload, (dict, list)):
            walk(payload, sec)
    return out


def build_entity_payload(
    entities: list[dict[str, Any]],
    *,
    views: dict[str, Any],
    word_geometry: list[dict[str, Any]],
    links: list[dict[str, Any]] | None = None,
    agent_trace: list[dict[str, Any]] | None = None,
    scorecards: list[dict[str, Any]] | None = None,
    repair_log: list[dict[str, Any]] | None = None,
    vmaw_log: list[dict[str, Any]] | None = None,
    binding_items: list[dict[str, Any]] | None = None,
    flagged_refs: set[str] | None = None,
    field_rationales: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Precompute everything the entity-explorer canvas needs per entity:
    page, highlight boxes, linked-entity boxes, status (review/accepted), and the
    field trace. Pure — `views` maps block_id → an object with .bbox/.page_number/.text."""
    flagged_refs = flagged_refs or set()
    field_rationales = field_rationales or {}

    def _boxes(e: dict[str, Any]) -> tuple[int | None, list[list[float]]]:
        page = None
        out: list[list[float]] = []
        for bid in (e.get("candidate_block_ids") or []):
            bv = views.get(bid)
            if not bv or not getattr(bv, "bbox", None):
                continue
            if page is None:
                page = bv.page_number
            if bv.page_number != page:
                continue
            out.extend(word_boxes_for_entity(
                word_geometry, page=bv.page_number, block_bbox=bv.bbox,
                surface=e.get("surface") or "", block_text=getattr(bv, "text", "")))
        return page, out

    boxes_by_ref: dict[str, list[list[float]]] = {}
    page_by_ref: dict[str, int | None] = {}
    for e in entities:
        pg, bx = _boxes(e)
        boxes_by_ref[e["ref"]] = bx
        page_by_ref[e["ref"]] = pg

    def _toks(ref: Any) -> set[str]:
        return set(_TOKEN_RE.findall(str(ref or "")))

    def _flagged(ref: str) -> bool:
        # a scalar field is "review" if it sits under (or over) a flagged record ref
        return any(ref == fr or ref.startswith(str(fr) + ".") or str(fr).startswith(ref + ".")
                   for fr in flagged_refs)

    payload: list[dict[str, Any]] = []
    for e in entities:
        ref = e["ref"]
        page = page_by_ref.get(ref)
        et = _toks(ref)
        linked: list[list[float]] = []
        linked_refs: list[str] = []
        linked_labels: list[str] = []
        def _add_link(other_ref: str) -> None:
            if other_ref == ref:
                return
            if other_ref not in linked_refs:
                linked_refs.append(other_ref)
                lbl = next((x.get("label") for x in entities if x["ref"] == other_ref), None)
                linked_labels.append(lbl or other_ref)
            if page_by_ref.get(other_ref) == page:                # drawable only if same page
                linked.extend(boxes_by_ref.get(other_ref, []))

        # (a) explicit linker relationships (tested↔result, biomarker↔specimen, …)
        for lk in (links or []):
            a, b = lk.get("from_ref"), lk.get("to_ref")
            other = b if (_toks(a) & et) else (a if (_toks(b) & et) else None)
            if not other:
                continue
            ot = _toks(other)
            for e2 in entities:
                if e2["ref"] != ref and (_toks(e2["ref"]) & ot):
                    _add_link(e2["ref"])

        # (b) structural (within-record) siblings — every field of the SAME top-level
        # record is related: a biomarker + its findings/variant_detail; a specimen
        # finding + its specimen[]/staging/nodal/gross/micro/histologic sub-fields; a
        # clinical-history entry; etc. The LINKER emits CROSS-record relationships
        # (staging_of_specimen, tested_to_result, …) — already handled in (a) — but not
        # these WITHIN-record ones, so we synthesise them generically from the record
        # root. Flat sections (report_metadata) have no array index → not mass-linked.
        root = _record_root(ref)
        if root:
            for e2 in entities:
                r2 = e2["ref"]
                if r2 != ref and (r2.startswith(root + ".") or r2.startswith(root + "[")):
                    _add_link(r2)

        # De-stack the page overlay: dedupe linked boxes AND drop any that coincide
        # with the selected entity's OWN box — otherwise (esp. with block-box fallback
        # where many record siblings resolve to the SAME block) the linked boxes pile
        # up on one region and bury the selected highlight under a dark stack.
        own_boxes = boxes_by_ref.get(ref, [])
        own_set = {tuple(round(c, 4) for c in b) for b in own_boxes}
        seen: set = set()
        linked_dedup: list[list[float]] = []
        for b in linked:
            key = tuple(round(c, 4) for c in b)
            if key in own_set or key in seen:
                continue
            seen.add(key)
            linked_dedup.append(b)

        payload.append({
            "ref": ref, "label": e.get("label"), "section": e.get("section"),
            "surface": e.get("surface"),
            "status": "review" if _flagged(ref) else "accepted",
            "page": page or 1, "boxes": own_boxes, "linked_boxes": linked_dedup,
            "linked_refs": linked_refs, "linked_labels": linked_labels,
            "trace": assemble_field_trace(
                # trace_section = the OUR-envelope section (for filtering agent_trace);
                # `section` may have been relabelled to a production location by the
                # Production browser, which would otherwise drop every extraction step.
                ref=ref, section=e.get("trace_section") or e.get("section"),
                agent_trace=agent_trace, scorecards=scorecards,
                repair_log=repair_log, vmaw_log=vmaw_log, binding_items=binding_items,
                links=links, field_rationale=field_rationales.get(ref, "")),
        })
    return payload


# bookkeeping keys that are NOT extracted entities (counts, scores, internal flags)
_DENY_KEYS = {
    "occurrences", "needs_review", "review_reason", "superseded", "vmaw_confirmed",
    "sme_review", "llm_confidence_score", "page_numbers", "page_number", "count", "provenance",
    "total_pages", "block_id", "page", "char_start", "char_end",
    "count_of_extracted_objects", "count_of_tested_biomarkers", "count_of_specimen_findings",
}


def _subtree_blocks(obj: Any) -> list[str]:
    """All occurrence block_ids anywhere under `obj` (dedup, order-preserving)."""
    out: list[str] = []

    def rec(o: Any) -> None:
        if isinstance(o, dict):
            for x in (o.get("occurrences") or []):
                if isinstance(x, dict) and x.get("block_id"):
                    out.append(x["block_id"])
            for k, v in o.items():
                if k != "occurrences":
                    rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)

    rec(obj)
    return list(dict.fromkeys(out))


def enumerate_entities(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    """EVERY extracted value → a selectable entity {ref, section, surface,
    candidate_block_ids, label}. Scalar fields (biomarker names, report metadata,
    tested biomarkers, results, staging, …) all qualify — bookkeeping keys (counts,
    confidence, internal flags) are skipped. Block provenance is inherited from the
    nearest enclosing record's `occurrences` so a value can still be highlighted."""
    out: list[dict[str, Any]] = []

    def is_value(v: Any) -> bool:
        return isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip() != ""

    def walk(obj: Any, ref: str, section: str, inherited: list[str]) -> None:
        if isinstance(obj, dict):
            # provenance-wrapped field: {value, type, block_id, page, rationale}.
            # Emit ONLY the value (with its own block); don't surface the wrapper's
            # bookkeeping keys as entities.
            if "value" in obj and not isinstance(obj["value"], (dict, list)):
                val = obj["value"]
                if is_value(val):
                    blk = [str(obj["block_id"])] if obj.get("block_id") not in (None, "") \
                        else (_subtree_blocks(obj) or inherited)
                    key = ref.split(".")[-1].split("[")[0]
                    out.append({"ref": ref, "section": section, "kind": "extracted",
                                "surface": str(val), "candidate_block_ids": list(blk),
                                "label": f"{key}: {str(val)[:40]}"})
                return
            # the sibling `provenance` (array OR legacy map) attributes a block to each
            # scalar field. Normalise both shapes to {field_name: entry}.
            prov = {fn: meta for fn, meta in iter_provenance(obj.get("provenance"))}
            blocks = _subtree_blocks(obj) or inherited     # this record's provenance
            for k, v in obj.items():
                if k in _DENY_KEYS or str(k).startswith("count_of_"):
                    continue
                cref = f"{ref}.{k}"
                if isinstance(v, (dict, list)):
                    walk(v, cref, section, blocks)
                elif is_value(v):
                    fb = list(blocks)
                    pk = prov.get(k)
                    if isinstance(pk, dict) and pk.get("block_id") not in (None, ""):
                        fb = [str(pk["block_id"])]
                    out.append({"ref": cref, "section": section, "kind": "extracted",
                                "surface": str(v), "candidate_block_ids": fb,
                                "label": f"{k}: {str(v)[:40]}"})
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                cref = f"{ref}[{i}]"
                if isinstance(v, (dict, list)):
                    walk(v, cref, section, inherited)
                elif is_value(v):                          # list of scalars (e.g. tested_biomarkers)
                    out.append({"ref": cref, "section": section, "kind": "extracted",
                                "surface": str(v), "candidate_block_ids": list(inherited),
                                "label": str(v)[:48]})

    for sec, payload in (extraction or {}).items():
        if isinstance(payload, (dict, list)):
            walk(payload, sec, sec, [])
    return out
