"""
production_browser_view — the production-schema output in the SAME interactive
canvas as the Entity browser (P3-M9).

The production file (`extraction_production.json`) deliberately drops provenance /
occurrences, so it has no geometry or trace of its own. We therefore drive this
browser from the **v3 envelope** (which keeps blocks + trace), reuse the full
entity-explorer machinery (page highlight, linked colors, agent trace, Review/
Accepted filter, Technical / Binding toggles), and simply (a) relabel each entity
to its PRODUCTION location and (b) hide fields that don't survive into production.
"""

from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components

import yaml

from transform.to_production import admin_inverse, production_label
from ui.phase1.block_view import build_block_views, find_source_pdf
from ui.phase1.components.entity_explorer import build_entity_explorer_html
from ui.phase1.data_layer import load_artifact, load_extraction
from ui.phase1.evidence import build_entity_payload, enumerate_entities, field_rationale_map
from ui.phase1.page_render import render_pages

_MAPPING = "config/production_mapping.yaml"


@st.cache_data(show_spinner=False)
def _pages(pdf_str: str) -> list[dict]:
    from pathlib import Path
    return render_pages(Path(pdf_str))


def render(*, run) -> None:
    doc_id = run.doc_id
    prod = load_artifact(doc_id, "extraction_production")
    if prod is None:
        st.info(
            "No production output for this run yet. Generate it from the saved "
            "extraction:\n\n```bash\nmake to-production DOC=" + doc_id + "\n```\n\n"
            "Then hit 🔄 Reload."
        )
        return

    # drive from the v3 envelope (it carries the geometry + trace)
    v3 = (load_artifact(doc_id, "extraction_reviewed")
          or load_extraction(doc_id) or load_artifact(doc_id, "extraction_v2") or {})
    admin_inv = admin_inverse(yaml.safe_load(open(_MAPPING, encoding="utf-8")) or {})

    kept = []
    dropped = 0
    for e in enumerate_entities(v3):
        plabel = production_label(e["ref"], admin_inv)
        if plabel is None:
            dropped += 1
            continue
        e2 = dict(e)
        e2["trace_section"] = e["section"]         # OUR-envelope section — keeps the agent trace
        e2["label"] = plabel                       # show the PRODUCTION location
        e2["section"] = plabel.split(" › ")[0]     # production section (display only)
        kept.append(e2)

    if not kept:
        st.warning("No production-mapped entities with provenance for this run. "
                   "(Re-run through v3 so geometry/trace exist.)")
        return

    flagged_refs = {it.get("ref") for it in (load_artifact(doc_id, "escalation_queue") or [])}
    views = {v.block_id: v for v in build_block_views(doc_id)}
    pdf_path = find_source_pdf(doc_id)
    pages = _pages(str(pdf_path)) if pdf_path else []
    verification = load_artifact(doc_id, "verification_v2")
    links = verification.get("links") if isinstance(verification, dict) else None
    scorecards = verification.get("scorecards") if isinstance(verification, dict) else None

    payload = build_entity_payload(
        kept, views=views,
        word_geometry=load_artifact(doc_id, "word_geometry") or [],
        links=links or [],
        agent_trace=load_artifact(doc_id, "agent_trace") or [],
        scorecards=scorecards or [],
        repair_log=load_artifact(doc_id, "repair_log") or [],
        vmaw_log=load_artifact(doc_id, "vmaw_log") or [],
        binding_items=load_artifact(doc_id, "binding_items") or [],
        flagged_refs=flagged_refs,
        field_rationales=field_rationale_map(v3),
    )

    st.caption(f"Production schema · {len(kept)} mapped fields · {dropped} v3-only fields hidden "
               "· select a field → highlight + linked + agent trace (same as Entity browser)")
    components.html(build_entity_explorer_html(payload, pages, height=760), height=900, scrolling=True)
