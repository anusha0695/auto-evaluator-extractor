"""
sme_review_view — the SME review queue + evidence + decision screen (P3-M8d).

Surfaces only what the pipeline couldn't settle on its own (the post-VMAW
`escalation_queue`). For the selected item it auto-jumps to the source page,
highlights the flagged entity (a synced text strip with the surface
<mark>-highlighted by character match — always works — plus a pixel-tight overlay
on the page image where OCR word geometry exists), colors link endpoints, shows
WHY it was flagged, the extractor value vs VMAW's proposal, the per-field agent
trace (technical / plain) right beside the decision, and Approve / Edit / Keep
actions that write `extraction_reviewed.json` + append `sme_decisions.json`
(original immutable).
"""

from __future__ import annotations

import html
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from ui.phase1.block_view import build_block_views, find_source_pdf
from ui.phase1.data_layer import load_artifact, load_extraction
from ui.phase1.evidence import build_evidence_html, render_trace
from ui.phase1.field_trace import why as why_flagged
from ui.phase1.page_render import render_pages
from ui.phase1.sme_decisions import (
    append_sme_decision,
    apply_sme_decision,
    build_review_model,
    write_reviewed_extraction,
)


@st.cache_data(show_spinner=False)
def _pages(pdf_str: str) -> list[dict]:
    from pathlib import Path
    return render_pages(Path(pdf_str))


def render(*, run) -> None:
    doc_id = run.doc_id

    # Distinguish "no repair-loop artifacts at all" (v2 run) from "queue is empty".
    raw_queue = load_artifact(doc_id, "escalation_queue")
    if raw_queue is None:
        st.info(
            "This run has no review queue yet. The queue, agent trace, and VMAW "
            "suggestions are produced by the **Phase-3 repair-loop graph (v3)**. "
            "Re-process the document through v3 to populate this tab:\n\n"
            "```bash\n"
            "make run-local PDF=./data/actual_docs/demo.pdf ARGS=\"--version v3\"\n"
            "# or:  PYTHONPATH=. python scripts/process_local.py --pdf ./data/actual_docs/demo.pdf --version v3\n"
            "```\n\n"
            "Then hit **🔄 Reload** in the sidebar."
        )
        return

    model = build_review_model(doc_id, load=load_artifact)
    items = model["items"]
    c = model["counts"]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("SME queue", c["queue"])
    m2.metric("VMAW proposals", c["proposals"])
    m3.metric("Unresolved", c["unresolved"])
    m4.metric("VMAW auto-applied", c["auto_applied"])

    if not items:
        st.success("Nothing needs review — every section was auto-accepted or auto-resolved. ✅")
        return

    views = {v.block_id: v for v in build_block_views(doc_id)}
    word_geometry = load_artifact(doc_id, "word_geometry") or []
    pdf_path = find_source_pdf(doc_id)
    pages = _pages(str(pdf_path)) if pdf_path else []
    pages_by_num = {p["page_number"]: p for p in pages}

    tcol1, tcol2, tcol3 = st.columns(3)
    technical = tcol1.toggle("Technical view", value=False,
                             help="Plain-language for reviewers · technical shows agents, nodes, refs, verdicts.")
    show_binding = tcol2.toggle("Show binding checks", value=False,
                                help="Reveal the binding-verifier's per-check evidence in the trace.")
    show_flowchart = tcol3.toggle("Show pipeline flowchart", value=False,
                                  help="Render the pipeline diagram (triage repair loop + VMAW branch) above the trace.")

    col_q, col_main = st.columns([1, 3], gap="medium")
    with col_q:
        st.caption("Queue · proposals first")
        labels = [_queue_label(it) for it in items]
        sel = st.radio("queue", options=list(range(len(items))),
                       format_func=lambda i: labels[i], label_visibility="collapsed")

    item = items[sel]

    with col_main:
        col_ev, col_dec = st.columns([1.05, 1], gap="medium")

        with col_ev:
            st.markdown(f"**Source** · page auto-jumped · `{html.escape(item.get('section') or '')}`")
            prop0 = item.get("vmaw_proposal") or {}
            cited = list(prop0.get("citation_block_ids") or []) or list(item.get("candidate_block_ids") or [])
            surface = str(prop0.get("value") or item.get("detail") or "")
            components.html(build_evidence_html(
                cited_block_ids=cited, surface=surface, views=views,
                word_geometry=word_geometry, pages_by_num=pages_by_num),
                height=460, scrolling=True)

        with col_dec:
            st.markdown(f"`{html.escape(item.get('kind') or '')}`")
            st.markdown(item["_technical"] if technical else item["_plain"])
            # WHY it was flagged (esp. for links)
            st.info(("**Why flagged:** " if not technical else "**defect:** ")
                    + why_flagged(item, mode="technical" if technical else "plain"))

            prop = item.get("vmaw_proposal") or {}
            if prop.get("value") is not None:
                st.markdown(f"**Suggestion:** `{html.escape(str(prop['value']))}`"
                            + (" · grounded" if prop.get("grounded") else " · not grounded")
                            + (" · contested" if prop.get("contested") else ""))

            edited = st.text_input("Value", value=str(prop.get("value") or ""),
                                   label_visibility="collapsed", placeholder="value…")
            b1, b2, b3 = st.columns(3)
            approve = b1.button("✓ Approve", use_container_width=True, disabled=prop.get("value") is None)
            edit = b2.button("✎ Save", use_container_width=True)
            keep = b3.button("⚑ Keep", use_container_width=True)
            if approve or edit or keep:
                _commit_decision(doc_id, item, approve=approve, edit=edit, keep=keep,
                                 edited=edited, proposal_value=prop.get("value"))

        # agent trace — right under the decision, for the selected item.
        # `show_flowchart=True` renders the pipeline diagram (triage repair loopback +
        # VMAW branch) above the per-field timeline as orientation.
        st.markdown("##### How this field was processed")
        render_trace(
            st, item.get("_trace") or [],
            technical=technical, show_binding=show_binding,
            ref=item.get("ref"), value=(item.get("proposal") or {}).get("value"),
            show_flowchart=show_flowchart,
        )


def _queue_label(it: dict[str, Any]) -> str:
    tag = "● proposal" if it.get("vmaw_proposal") else ("○ unresolved" if it.get("vmaw_note") else "· flagged")
    return f"{tag} — {(it.get('section') or it.get('kind') or 'item')}"


def _commit_decision(doc_id, item, *, approve, edit, keep, edited, proposal_value) -> None:
    extraction = (load_artifact(doc_id, "extraction_reviewed")
                  or load_extraction(doc_id) or load_artifact(doc_id, "extraction_v2") or {})
    if approve:
        action, value = "approve", None
    elif edit:
        action, value = "edit", edited
    else:
        action, value = "keep_flagged", None
    reviewed, decision = apply_sme_decision(
        extraction, ref=item.get("ref"), action=action, value=value, proposal_value=proposal_value)
    write_reviewed_extraction(doc_id, reviewed)
    append_sme_decision(doc_id, decision)
    st.success(f"Recorded: {action} → {decision.get('applied_value')!r}. "
               "Reviewed extraction + decision log updated.")
