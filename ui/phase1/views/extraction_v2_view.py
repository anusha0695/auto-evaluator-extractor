"""
Phase 2a extraction view — renders the v3 envelope a graph_linear run produced.

Reads two artifacts the linker/verifier nodes persist EVERY run (regardless of
verdict), so the SME can review even sme_flag/fixable docs:
  local_runs/artifacts/<doc>/extraction_v2.json   — the full v3 envelope
  local_runs/artifacts/<doc>/verification_v2.json  — PHI-safe scorecards + links

Shows: genomic variants (with occurrences provenance + needs_review), molecular
biomarker findings[] grouping, the tested panel, clinical_information, the
variant↔panel links, and the verifier verdicts. `significant_findings` is shown
as "(Phase 2b — not yet extracted)" when empty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_v2_artifacts(doc_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pure loader (no Streamlit) — returns (extraction_envelope, verification).
    Either may be None if the doc hasn't been run through graph_linear yet."""
    from core.persistence import load_storage_config

    art_dir: Path | None = None
    for phase in ("phase_2", "phase_1"):
        try:
            cfg = load_storage_config(phase)
            d = Path(cfg.local_dir_resolved) / "artifacts" / doc_id
            if d.exists():
                art_dir = d
                break
        except Exception:
            continue
    if art_dir is None:
        return None, None

    def _read(name: str) -> dict[str, Any] | None:
        p = art_dir / name
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    return _read("extraction_v2.json"), _read("verification_v2.json")


def render(*, run: Any) -> None:  # pragma: no cover (Streamlit UI)
    import streamlit as st

    doc_id = getattr(run, "doc_id", None) or "unknown"
    envelope, verification = load_v2_artifacts(doc_id)

    if envelope is None:
        st.info(
            f"No Phase 2a (v3) extraction found for **{doc_id}**.\n\n"
            "Run the v2 pipeline first:\n```bash\n"
            "make run-local PDF=./data/actual_docs/<doc>.pdf PHASE=2\n```"
        )
        return

    # ----- Verdict / verifier banner -------------------------------------
    if verification:
        binding = verification.get("binding_verifier") or {}
        cards = verification.get("scorecards") or []
        failed = [c for c in cards if not c.get("passed", True)]
        cols = st.columns(3)
        cols[0].metric("Verifiers passed", f"{len(cards) - len(failed)}/{len(cards)}")
        cols[1].metric("Binding refuted", binding.get("refuted", 0))
        cols[2].metric("Binding uncertain", binding.get("uncertain", 0))
        with st.expander("Verifier scorecards", expanded=bool(failed)):
            for c in cards:
                icon = "✅" if c.get("passed") else "❌"
                st.write(f"{icon} **{c.get('verifier_name')}** — {c.get('notes','')}")
                if c.get("error_locs"):
                    st.caption(f"error locations: {c['error_locs']}")

    # ----- Genomic variants (v4: REVIVED separate Genomic_Variant_umbrella) ---
    # v3 has no such section → the block is skipped. v4 routes gene SEQUENCE variants
    # here (out of the biomarker umbrella), one FLAT record per variant.
    variants = (envelope.get("Genomic_Variant_umbrella") or {}).get("Genomic_Variants") or []
    if variants:
        st.subheader(f"🧬 Genomic variants ({len(variants)})")
        st.dataframe([
            {
                "gene": v.get("gene_studied"), "method": v.get("method"), "result": v.get("result"),
                "c.": v.get("coding_dna_change"), "p.": v.get("amino_acid_change"),
                "g.": v.get("genomic_dna_change"), "VAF": v.get("variant_allele_frequency"),
                "significance": v.get("clinical_significance"),
                "source_class": v.get("genomic_source_class"),
                "needs_review": bool(v.get("needs_review")),
            }
            for v in variants
        ], use_container_width=True)

    # ----- Biomarkers ---------------------------------------------------------
    # Shape-tolerant: v3 groups results under a nested `findings[]` (a sequence variant
    # carries a nested `variant_detail`); v4 is FLAT (the record IS the result — no
    # findings[], no variant_detail; variants live in Genomic_Variant_umbrella above).
    biomarkers = (envelope.get("other_molecular_biomarker_umbrella") or {}).get("other_molecular_biomarkers") or []
    st.subheader(f"🔬 Biomarkers ({len(biomarkers)})")
    for bm in biomarkers:
        flag = " ⚠️ needs_review" if bm.get("needs_review") else ""
        cls = bm.get("biomarker_class")
        cls_tag = f"  ·  _{cls}_" if cls else ""
        st.markdown(f"**{bm.get('biomarker_name')}**{cls_tag}{flag}")
        rows = []
        findings = bm.get("findings")
        if isinstance(findings, list) and findings:                  # v3 nested shape
            for f in findings:
                vd = f.get("variant_detail") or {}
                variant = None
                if isinstance(vd, dict) and vd:
                    variant = vd.get("amino_acid_change") or vd.get("coding_dna_change")
                rows.append({
                    "method": f.get("method"), "result": f.get("result"),
                    "variant": variant,  # 🧬 set only for sequence-variant findings
                    "interpretation": f.get("interpretation"), "assertion": f.get("assertion"),
                    "method_source": f.get("method_source_type"),
                    "occurrences": len(f.get("occurrences") or []),
                })
        else:                                                        # v4 FLAT shape
            rows.append({
                "method": bm.get("method"), "result": bm.get("result"),
                "reference_range": bm.get("reference_range"),
                "interpretation": bm.get("interpretation"),
            })
        st.dataframe(rows, use_container_width=True)

    # ----- Tested panel ---------------------------------------------------
    panel = (envelope.get("tested_biomarker_umbrella") or {}).get("tested_biomarkers") or []
    st.subheader(f"🧪 Tested panel ({len(panel)})")
    if panel:
        st.write(", ".join(map(str, panel)))

    # ----- Clinical information ------------------------------------------
    # Guarded: in v4 this section is DISABLED (absent from the envelope) — don't render
    # an empty null card. v2/v3 always carry the key, so it renders as before.
    if "clinical_information" in envelope:
        clin = envelope.get("clinical_information") or {}
        st.subheader("📋 Clinical information")
        st.json({
            "reason_for_study": clin.get("reason_for_study"),
            "clinical_finding_details": clin.get("clinical_finding_details"),
            "clinical_history": clin.get("clinical_history"),
        })

    # ----- Cross-section links -------------------------------------------
    links = (verification or {}).get("links") or []
    st.subheader(f"🔗 Cross-section links ({len(links)})")
    if links:
        st.dataframe([
            {"type": l.get("type"), "from": l.get("from_ref"), "to": l.get("to_ref"),
             "method": l.get("method"), "rationale": l.get("rationale")}
            for l in links
        ], use_container_width=True)

    # ----- significant_findings (Phase 2b) -------------------------------
    # Guarded: DISABLED in v4 (absent from the envelope) → skip the section entirely so
    # a disabled team renders nothing. v2/v3 always carry the key, so it renders as before.
    if "significant_findings" in envelope:
        sf = (envelope.get("significant_findings") or {}).get("specimen_findings") or []
        st.subheader("🧫 Significant findings")
        if sf:
            for i, entry in enumerate(sf):
                specs = entry.get("specimen") or []
                label = ", ".join(f"{s.get('specimen_id')}: {s.get('tissue_type')}" for s in specs) or f"specimen {i}"
                st.markdown(f"**{label}**")
                tn = entry.get("pTNM_staging_details") or {}
                ln = entry.get("lymph_node_details") or {}
                st.json({
                    "procedure": (entry.get("procedure_details") or {}).get("procedure"),
                    "histologic_findings": [h.get("finding") for h in (entry.get("histologic_findings") or [])],
                    "pTNM_stage": tn.get("pTNM_stage"),
                    "staging_system_version": tn.get("staging_system_version"),
                    "lymph_nodes": {"status": ln.get("lymph_node_status"),
                                    "examined": ln.get("number_of_lymph_nodes_examined"),
                                    "positive": ln.get("number_of_lymph_nodes_positive")},
                })
        else:
            st.caption("No surgical-pathology content in this report (e.g. a molecular/PCR assay) — `specimen_findings` empty, as expected.")
