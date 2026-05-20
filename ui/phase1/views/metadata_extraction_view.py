"""Metadata Extraction view — extracted `report_metadata` JSON + ground-truth diff."""

from __future__ import annotations

from typing import Any

import streamlit as st

from ui.phase1.components.json_tree import render_json, render_report_metadata_table
from ui.phase1.data_layer import RunSummary, load_ground_truth


# Field-level help text — drawn from the prompt rules so the UI doubles as
# Phase 1 documentation for the SME reviewer.
_FIELD_NOTES: dict[str, str] = {
    "Patient_MRN": (
        "STRICT MRN rule: only populated when the source literally labels the "
        "value as `MRN`, `Medical Record #`, or `Med Rec #`. ID / Accession / "
        "Case Number fields do NOT count and must yield null."
    ),
    "Vendor_Name": (
        "VERBATIM_OR_INFERRED: prefer the explicit lab wordmark / copyright "
        "footer. Inference allowed only from product markers (`xT`→Tempus, "
        "`FoundationOne`→Foundation Medicine, `xGEN`→Caris)."
    ),
    "Practice_Name": (
        "Ordering clinic name (NOT the lab vendor). Critical disambiguation: "
        "`Hematology & Oncology Consultants` is the practice; `NeoGenomics` "
        "is the vendor."
    ),
    "Collection_Date": "DERIVED to ISO YYYY-MM-DD. Drops time-of-day + timezone.",
    "Received_Date": "DERIVED to ISO YYYY-MM-DD.",
    "Report_Date":  "DERIVED to ISO YYYY-MM-DD.",
    "Patient_DOB":  "DERIVED to ISO YYYY-MM-DD. Drops trailing sex fragment (e.g. `/ M`).",
}


def render(*, run: RunSummary, extraction: dict[str, Any] | None) -> None:
    st.header(f"🔍 Extracted metadata — {run.doc_id}")
    st.caption(
        "The `report_metadata` umbrella section of the schema-v2 envelope. "
        "Other umbrellas (`Genomic_Variant_umbrella` etc.) are empty in Phase 1."
    )

    if extraction is None:
        st.info("No extraction was committed for this run.")
        return

    meta = extraction.get("report_metadata") or {}
    if not meta:
        st.warning("Envelope has no `report_metadata` section.")
        return

    # ----- Ground-truth diff toggle ---------------------------------------
    gt = load_ground_truth(run.doc_id)
    if gt is not None:
        show_diff = st.toggle(
            "Compare against ground_truth",
            value=True,
            help="Adds an Expected column and ✓/✗ per row.",
        )
    else:
        show_diff = False
        st.caption("_(No `ground_truth/<doc>.json` found for this doc.)_")

    # ----- The table ------------------------------------------------------
    render_report_metadata_table(
        extraction=meta,
        ground_truth=gt if show_diff else None,
    )

    # ----- Quick stats when diffing ---------------------------------------
    if show_diff and gt is not None:
        from ui.phase1.components.json_tree import _values_match

        total = sum(1 for k in gt if k != "llm_confidence_score")
        passed = sum(
            1 for k, v in gt.items()
            if k != "llm_confidence_score" and _values_match(v, meta.get(k))
        )
        rate = passed / max(total, 1)
        c1, c2, c3 = st.columns(3)
        c1.metric("Fields scored", total)
        c2.metric("Passing", passed)
        c3.metric(
            "Pass rate",
            f"{rate*100:.1f}%",
            delta="≥ 80% gate" if rate >= 0.80 else f"below 80%",
            delta_color="normal" if rate >= 0.80 else "inverse",
        )

    # ----- Per-field notes panel ------------------------------------------
    with st.expander("📖 Field notes — Phase 1 extraction rules", expanded=False):
        for field, note in _FIELD_NOTES.items():
            st.markdown(f"**`{field}`** — {note}")

    # ----- Raw JSON --------------------------------------------------------
    with st.expander("Raw JSON", expanded=False):
        render_json(meta, expanded=True)

    # ----- LLM self-confidence --------------------------------------------
    conf = meta.get("llm_confidence_score")
    if conf is not None:
        st.caption(f"Model self-reported confidence: **{float(conf):.2f}**")
