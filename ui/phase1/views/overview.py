"""Overview page — run metadata, verdict, costs, latency, raw JSON envelope."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from ui.phase1.data_layer import RunSummary, load_artifact


def render(*, run: RunSummary, extraction: dict[str, Any] | None) -> None:
    st.header(f"📄 {run.doc_id}")
    st.caption(f"Run {run.run_id} · {run.completed_at}")

    # ----- Verdict banner -------------------------------------------------
    verdict = run.verdict or "(none)"
    if verdict == "auto_accept":
        st.success(f"✅ **{verdict}**  —  {run.verdict_reason}")
    elif verdict == "sme_flag":
        st.warning(f"⚠️  **{verdict}**  —  {run.verdict_reason}")
    elif verdict == "errored":
        st.error(f"❌ **{verdict}**  —  {run.verdict_reason}")
    else:
        st.info(f"**{verdict}**  —  {run.verdict_reason}")

    # ----- KPI strip ------------------------------------------------------
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pipeline version", run.pipeline_version or "—")
    c2.metric("Latency", f"{run.latency_ms / 1000:.1f}s")
    c3.metric("Cost", f"${run.cost_usd:.4f}")
    c4.metric(
        "Verdict",
        verdict,
        delta=("auto" if verdict == "auto_accept" else None),
    )

    # ----- Artifacts links ------------------------------------------------
    st.divider()
    st.subheader("Artifacts")
    st.caption(f"Prefix: `{run.artifacts_prefix}`")
    arts = []
    for kind in ("docai_raw", "block_profiles", "parser_hypothesis"):
        if load_artifact(run.doc_id, kind) is not None:
            arts.append(f"`{kind}.json`")
    st.write("Files: " + (", ".join(arts) if arts else "_none found_"))

    # ----- Full extraction envelope --------------------------------------
    st.divider()
    st.subheader("Extraction envelope (full schema-v2)")
    if extraction is None:
        st.info("No extraction was committed for this run (probably an SME flag).")
    else:
        with st.expander("Show full JSON", expanded=False):
            st.json(extraction, expanded=False)

        # Quick stats on what got extracted
        meta = extraction.get("report_metadata") or {}
        n_metadata = sum(1 for v in meta.values() if v not in (None, ""))
        biomarkers = (extraction.get("other_molecular_biomarker_umbrella") or {}).get("other_molecular_biomarkers") or []
        n_biomarkers = (extraction.get("other_molecular_biomarker_umbrella") or {}).get("count", len(biomarkers))
        # sequence variants are biomarker findings carrying a variant_detail (v3 merge)
        n_variants = sum(
            1 for bm in biomarkers for f in (bm.get("findings") or [])
            if isinstance(f, dict) and f.get("variant_detail")
        )
        n_tested = (extraction.get("tested_biomarker_umbrella") or {}).get("count_of_tested_biomarkers", 0)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("metadata fields populated", n_metadata)
        c2.metric("biomarkers", n_biomarkers)
        c3.metric("of which variants", n_variants)
        c4.metric("tested biomarkers", n_tested)
