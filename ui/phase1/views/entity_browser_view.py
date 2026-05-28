"""
entity_browser_view — Block-explorer-style browser for ALL entities (P3-M8).

One interactive canvas (read-only): a Review / Accepted filter, the entity list on
the left, the real page on top with the selected entity highlighted + its linked
entities in a different color, a draggable divider, and the collapsible agent trace
below (Technical / Binding toggles). Decisions stay in the Review queue tab.
"""

from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components

from ui.phase1.block_view import build_block_views, find_source_pdf
from ui.phase1.components.entity_explorer import build_entity_explorer_html
from ui.phase1.data_layer import load_artifact, load_extraction
from ui.phase1.evidence import build_entity_payload, enumerate_entities, field_rationale_map
from ui.phase1.page_render import render_pages


@st.cache_data(show_spinner=False)
def _pages(pdf_str: str) -> list[dict]:
    from pathlib import Path
    return render_pages(Path(pdf_str))


def render(*, run) -> None:
    doc_id = run.doc_id
    extraction = (load_artifact(doc_id, "extraction_reviewed")
                  or load_extraction(doc_id) or load_artifact(doc_id, "extraction_v2") or {})
    entities = enumerate_entities(extraction)
    if not entities:
        st.info("No extracted entities with source provenance for this run. "
                "Process a document through the pipeline, then hit 🔄 Reload.")
        return

    flagged_refs = {it.get("ref") for it in (load_artifact(doc_id, "escalation_queue") or [])}
    views = {v.block_id: v for v in build_block_views(doc_id)}
    pdf_path = find_source_pdf(doc_id)
    pages = _pages(str(pdf_path)) if pdf_path else []

    verification = load_artifact(doc_id, "verification_v2")
    links = verification.get("links") if isinstance(verification, dict) else None
    scorecards = verification.get("scorecards") if isinstance(verification, dict) else None

    payload = build_entity_payload(
        entities, views=views,
        word_geometry=load_artifact(doc_id, "word_geometry") or [],
        links=links or [],
        agent_trace=load_artifact(doc_id, "agent_trace") or [],
        scorecards=scorecards or [],
        repair_log=load_artifact(doc_id, "repair_log") or [],
        vmaw_log=load_artifact(doc_id, "vmaw_log") or [],
        binding_items=load_artifact(doc_id, "binding_items") or [],
        flagged_refs=flagged_refs,
        field_rationales=field_rationale_map(extraction),
    )

    n_review = sum(1 for p in payload if p["status"] == "review")
    st.caption(f"{len(payload)} extracted entities · {n_review} flagged for review · "
               "select an entity → highlight + linked entities; trace below")
    if st.toggle("Show pipeline flowchart", value=False, key="ent_show_flowchart",
                 help="Render the pipeline diagram (triage repair loop + VMAW branch) above the entity explorer."):
        from ui.phase1.field_view import pipeline_flow_svg
        components.html(
            f'<div style="background:#fff;padding:8px;border-radius:8px;">{pipeline_flow_svg()}</div>',
            height=440, scrolling=False,
        )
    components.html(build_entity_explorer_html(payload, pages, height=760), height=900, scrolling=True)
