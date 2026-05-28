"""
Phase 2 M5 verification gate — Linker (deterministic parts; no LLM).

Done-when (deterministic portion of PHASE_2_PLAN.md M5):
  - assemble: team section outputs → full v3 envelope (missing sections filled);
  - gene-key LINK: a variant links to the panel entry for the same gene, incl.
    alias resolution (HER2 variant ↔ ERBB2 panel);
  - SUPERSESSION detection: an addendum block naming a biomarker flags that
    finding needs_review (never silently rewrites);
  - count_of_extracted_objects is computed.

The contextual MERGE/LINK adjudication runs at the M8 checkpoint (injected LLM).

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m5_linker.py
"""

from __future__ import annotations

import sys

from agents.linker import Linker


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    linker = Linker()  # real offline hgnc_normalize; no LLM adjudicators

    # v3 merge: variants are biomarker entries carrying a variant_detail.
    # JAK2 + HER2 are variant biomarkers; BRAF is a plain IHC biomarker (no
    # variant_detail) — so BRAF must NOT get a variant_on_panel link even though
    # it is on the panel.
    sections = {
        "report_metadata": {"report_title": "Molecular Genetics", "llm_confidence_score": 0.9},
        "tested_biomarker_umbrella": {
            "count_of_tested_biomarkers": 3, "page_numbers": [4], "llm_confidence_score": 0.9,
            "tested_biomarkers": ["JAK2", "ERBB2", "BRAF"],
        },
        "other_molecular_biomarker_umbrella": {
            "count": 3, "llm_confidence_score": 0.9,
            "other_molecular_biomarkers": [
                {"biomarker_name": "JAK2", "findings": [
                    {"method": "PCR", "result": "Detected", "occurrences": [],
                     "variant_detail": {"amino_acid_change": "p.V617F"}}]},
                {"biomarker_name": "HER2", "findings": [   # alias → ERBB2
                    {"method": "FISH", "result": "Amplified", "occurrences": [],
                     "variant_detail": {"amino_acid_change": "p.X"}}]},
                {"biomarker_name": "BRAF", "findings": [    # plain IHC, NO variant_detail
                    {"method": "IHC", "result": "Positive", "occurrences": []}]},
            ],
        },
    }

    print("[1] assemble envelope (generic — emits exactly what teams produced)")
    res = linker.link(sections=sections, blocks=[], block_profiles=[])
    env = res.envelope
    expected = set(sections.keys()) | {"count_of_extracted_objects"}
    check("envelope emits exactly the produced sections (post-refactor: no v3 placeholders for teams that didn't run)",
          set(env) == expected, str(set(env) ^ expected))
    check("variant umbrella merged away (no genomic_variant_team in v3)", "Genomic_Variant_umbrella" not in env)
    check("count_of_extracted_objects computed via config/section_layout.yaml",
          env["count_of_extracted_objects"] == 3 + 3, str(env["count_of_extracted_objects"]))

    print("[2] gene-key links (incl. alias; variant_detail-gated)")
    types = [(l.from_ref, l.to_ref) for l in res.links if l.type == "variant_on_panel"]
    # JAK2 biomarker[0] ↔ panel[0]; HER2 biomarker[1] ↔ ERBB2 panel[1]; BRAF[2] not linked
    has_jak2 = any("other_molecular_biomarkers[0]" in f and "tested_biomarkers[0]" in t for f, t in types)
    has_her2 = any("other_molecular_biomarkers[1]" in f and "tested_biomarkers[1]" in t for f, t in types)
    check("JAK2 variant ↔ JAK2 panel linked", has_jak2, str(types))
    check("HER2 variant ↔ ERBB2 panel linked (alias)", has_her2, str(types))
    check("BRAF (no variant_detail) not over-linked", len(types) == 2, str(len(types)))

    print("[3] supersession detection on addendum block")
    res2 = linker.link(
        sections=sections,
        blocks=[{"block_id": "bX", "text": "Addendum: HER2 result amended to Negative."}],
        block_profiles=[{"block_id": "bX", "text_role": "addendum"}],
    )
    bms = res2.envelope["other_molecular_biomarker_umbrella"]["other_molecular_biomarkers"]
    her2 = next((b for b in bms if b.get("biomarker_name") == "HER2"), {})
    check("HER2 biomarker flagged needs_review", her2.get("needs_review") is True, str(her2.get("review_reason")))
    check("needs_review_ref recorded", len(res2.needs_review_refs) == 1, str(res2.needs_review_refs))

    print("[4] contextual links: dangling refs dropped, valid kept")
    def _fake_adj(*, envelope, blocks=None):
        return [
            {"from_ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]",   # valid
             "to_ref": "tested_biomarker_umbrella.tested_biomarkers[0]",
             "type": "finding_context", "rationale": "ctx", "evidence_block_ids": [], "confidence": 0.8},
            {"from_ref": "made_up.section[9]", "to_ref": "also.fake[3]",     # dangling
             "type": "bogus", "rationale": "hallucinated refs", "evidence_block_ids": [], "confidence": 0.9},
        ]
    linker2 = Linker(link_adjudicator=_fake_adj)
    res3 = linker2.link(sections=sections, blocks=[], block_profiles=[])
    ctx = [l for l in res3.links if l.method == "contextual"]
    check("only the valid contextual link kept (1)", len(ctx) == 1 and ctx[0].type == "finding_context",
          str([(l.type, l.from_ref) for l in ctx]))
    check("no dangling 'bogus' link committed", all(l.type != "bogus" for l in res3.links))

    print("-" * 60)
    if fails:
        print(f"M5 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M5 VERIFY: PASS — assemble + gene-key links + supersession detection.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
