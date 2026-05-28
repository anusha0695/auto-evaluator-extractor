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
    bands = model.get("bands") or {}
    c = model["counts"]

    # Top KPI row. The headline number is `blocks_workflow` (judgment +
    # unresolved) — those are the items that gate doc commit. review_light is
    # one-click rubber-stamp, drop_audit is periodic sampling — both surfaced
    # but not blocking.
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("⛔ Blocks workflow",
              c.get("blocks_workflow", c["queue"]),
              help="Items SME must clear before this doc can be committed (judgment + unresolved).")
    m2.metric("👤 Judgment", c.get("judgment", 0),
              help="Real decisions — contested or ungrounded.")
    m3.metric("🤔 Unresolved", c.get("unresolved", 0),
              help="VMAW exhausted every capability — needs investigation.")
    m4.metric("✅ Review-light", c.get("review_light", 0),
              help="VMAW landed grounded+uncontested proposals — quick rubber-stamp.")
    m5.metric("📦 Drop audit", c.get("drop_audit", 0),
              help="Records VMAW dropped — periodic audit, non-blocking.")

    auto_applied = c.get("auto_applied", 0)
    if auto_applied:
        st.caption(f"💡 VMAW auto-applied **{auto_applied}** item(s) silently — they never reached this queue.")

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

    # Banded queue. 4 expandable groups in priority order. Blocking bands
    # (judgment + unresolved) open by default; review_light + drop_audit
    # collapsed — open when ready. review_light has a batch-approve button.
    _BAND_META = {
        "judgment":     {"label": "👤 Judgment",     "default_open": True,
                         "blurb": "Real decisions — read the source, pick a value."},
        "unresolved":   {"label": "🤔 Unresolved",   "default_open": True,
                         "blurb": "VMAW exhausted EC + CITE + VA. Investigate from scratch."},
        "review_light": {"label": "✅ Review-light", "default_open": False,
                         "blurb": "VMAW found a clean grounded answer. Spot-check or batch-approve."},
        "drop_audit":   {"label": "📦 Drop audit",   "default_open": False,
                         "blurb": "Records VMAW dropped from the envelope. Audit periodically."},
    }

    col_q, col_main = st.columns([1, 3], gap="medium")
    with col_q:
        st.caption("Queue · grouped by band")
        # `items` is band_order-concatenated (judgment, then unresolved, then
        # review_light, then drop_audit), so a flat index over `items` is
        # equivalent to walking each band in order. We use ONE st.radio per
        # band (keyed independently) and translate the picked option back into
        # the flat index space stored in session state.
        sel = int(st.session_state.get("_sme_sel", 0))
        if sel >= len(items):
            sel = 0
            st.session_state["_sme_sel"] = 0

        offset = 0
        for band_name in ("judgment", "unresolved", "review_light", "drop_audit"):
            meta = _BAND_META[band_name]
            band_items = bands.get(band_name) or []
            with st.expander(f"{meta['label']} — {len(band_items)}",
                             expanded=meta["default_open"] and bool(band_items)):
                if not band_items:
                    st.caption("(none in this band)")
                else:
                    st.caption(meta["blurb"])
                    if band_name == "review_light":
                        if st.button(f"✓ Approve all {len(band_items)} review-light item(s)",
                                     key=f"approve_all_{band_name}", use_container_width=True,
                                     help="One-click approve every VMAW grounded+uncontested proposal in this band."):
                            _approve_all(doc_id, band_items)
                            st.rerun()
                    labels = [_queue_label(it) for it in band_items]
                    indices = list(range(offset, offset + len(band_items)))
                    in_band = (offset <= sel < offset + len(band_items))
                    picked = st.radio(f"queue_{band_name}", options=indices,
                                      format_func=lambda i, _labels=labels, _off=offset: _labels[i - _off],
                                      index=(sel - offset) if in_band else 0,
                                      label_visibility="collapsed", key=f"radio_{band_name}")
                    if in_band and picked != sel:
                        sel = picked
                        st.session_state["_sme_sel"] = sel
            offset += len(band_items)

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


def _approve_all(doc_id: str, band_items: list[dict[str, Any]]) -> None:
    """Batch-approve every item in the review_light band — VMAW's grounded +
    uncontested proposal is applied to the reviewed extraction, and one
    decision record per item is appended to the SME decision log. Idempotent
    on re-run (same approval recorded again is harmless)."""
    extraction = (load_artifact(doc_id, "extraction_reviewed")
                  or load_extraction(doc_id) or load_artifact(doc_id, "extraction_v2") or {})
    approved = 0
    for it in band_items:
        prop = it.get("vmaw_proposal") or {}
        if prop.get("value") is None:
            continue
        reviewed, decision = apply_sme_decision(
            extraction, ref=it.get("ref"), action="approve",
            proposal_value=prop.get("value"))
        extraction = reviewed     # chain — each approval works against the prior
        append_sme_decision(doc_id, decision)
        approved += 1
    if approved:
        write_reviewed_extraction(doc_id, extraction)
        st.success(f"Batch-approved {approved} review-light item(s). "
                   "Reviewed extraction + decision log updated.")
    else:
        st.info("No review-light items had a proposal value to approve.")


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
