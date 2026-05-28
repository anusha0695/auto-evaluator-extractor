"""
V4-M15 gate — V3 orphan is v4-aware + gene-key seed doesn't self-loop + Extractor
sub-steps render with specific plain-language strings.

Locks (offline, deterministic):

  [1] V3 orphan attachment check is now MULTI-SECTION. A candidate routed to the
      biomarker umbrella that's captured verbatim in Genomic_Variant_umbrella (e.g.
      "JAK2 tyrosine kinase" inside the variant's clinical_significance paragraph)
      is recognised as attached → no V3 verdict, no SME escalation, no drop-log.
  [2] A candidate routed to biomarker that is genuinely absent from every routable
      section AND has clean source still drops to the log (current behaviour
      preserved — don't flood SME with non-defects).
  [3] A candidate routed to biomarker that is genuinely absent AND has garbled
      source escalates as `V3 uncertain` (current behaviour preserved).
  [4] Gene-key seed skips i==j for self-typed link types (variant_superseded_by,
      superseded_by) — no self-loop on a single variant/biomarker record.
  [5] When TWO variants share the same gene, the seed DOES emit the
      variant_superseded_by link between them (i != j).
  [6] field_trace._plain_agent returns DISTINCT plain-language strings for each
      Extractor sub-step (thought / tool / tool-result) — not the generic line.

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m15_v3_orphan_and_selfloop.py
"""

from __future__ import annotations

import sys

from agents.link_binding_verifier import LinkBindingVerifier
from agents.linker import Linker
from agents.link_registry import LinkRegistry
from ui.phase1.field_trace import _plain_agent


def _v3(env, hyp, blocks):
    return [v for v in LinkBindingVerifier().verify(
        envelope=env, links=[], blocks=blocks, parser_hypothesis=hyp).verdicts
            if v.check == "V3"]


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] V3 orphan finds 'JAK2 tyrosine kinase' attached via the variant section")
    env_attached = {
        "Genomic_Variant_umbrella": {"Genomic_Variants": [
            {"gene_studied": "JAK2",
             "clinical_significance": "This mutation results in the constitutive activation of the JAK2 tyrosine kinase, present in polycythemia vera."}]},
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"tested_biomarkers": ["JAK2"]},
    }
    hyp_attached = {"candidates": [
        {"text": "JAK2 tyrosine kinase", "target_umbrella": "other_molecular_biomarker_umbrella",
         "entity_label": "GENE_OR_GENE_PRODUCT",
         "occurrences": [{"block_id": "62"}]},
    ]}
    blocks_attached = [{"block_id": "62", "text": "This mutation results in the constitutive activation of the JAK2 tyrosine kinase, present in polycythemia vera."}]
    v3 = _v3(env_attached, hyp_attached, blocks_attached)
    check("V3 emits NO verdict for 'JAK2 tyrosine kinase' (attached to variant section)",
          all("JAK2 tyrosine kinase" not in v.ref and "JAK2 tyrosine kinase" not in v.evidence for v in v3),
          str([(v.ref, v.verdict) for v in v3]))

    print("[2] genuinely absent + clean → still log-only (no SME verdict)")
    env_absent = {
        "Genomic_Variant_umbrella": {"Genomic_Variants": []},
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"tested_biomarkers": []},
    }
    hyp_absent = {"candidates": [
        {"text": "MRN", "target_umbrella": "other_molecular_biomarker_umbrella",
         "entity_label": "GENE_OR_GENE_PRODUCT",  # mis-routed; not in any section
         "occurrences": [{"block_id": "11"}]},
    ]}
    blocks_absent = [{"block_id": "11", "text": "MRN: 12345"}]  # clean source
    v3 = _v3(env_absent, hyp_absent, blocks_absent)
    check("no V3 verdict for unattached-but-clean candidate (logged only, dropped)",
          not any(v.ref == "candidate:MRN" for v in v3))

    print("[3] genuinely absent + garbled → V3 uncertain (escalate)")
    hyp_garb = {"candidates": [
        {"text": "J@K2~~|", "target_umbrella": "other_molecular_biomarker_umbrella",
         "entity_label": "GENE_OR_GENE_PRODUCT",
         "occurrences": [{"block_id": "99"}]},
    ]}
    blocks_garb = [{"block_id": "99", "text": "J@K2~~| smudge !!&&|/}{<>~"}]
    v3 = _v3(env_absent, hyp_garb, blocks_garb)
    check("V3 uncertain emitted when source is garbled",
          any(v.verdict == "uncertain" and "J@K2~~|" in v.ref for v in v3),
          str([(v.ref, v.verdict, v.evidence[:40]) for v in v3]))

    print("[4] gene-key seed: no self-loop on single variant (variant_superseded_by)")
    reg = LinkRegistry.from_path("config/link_registry_v4.yaml", max_tier=2)
    one_var = {
        "report_metadata": {},
        "Genomic_Variant_umbrella": {"count_of_Genomic_Variants": 1, "llm_confidence_score": None,
                                     "Genomic_Variants": [{"gene_studied": "JAK2"}]},
        "other_molecular_biomarker_umbrella": {"count_of_other_molecular_biomarkers": 0,
                                               "llm_confidence_score": None,
                                               "other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"count_of_tested_biomarkers": 1, "page_numbers": [],
                                      "llm_confidence_score": None, "tested_biomarkers": ["JAK2"]},
    }
    res = Linker(link_registry=reg).link(sections=one_var, blocks=[], block_profiles=[])
    types = [(L.type, L.from_ref, L.to_ref) for L in res.links]
    check("variant_superseded_by NOT emitted on a single variant",
          not any(t[0] == "variant_superseded_by" for t in types),
          str(types))
    check("variant_on_panel STILL emitted on the single variant",
          any(t[0] == "variant_on_panel" for t in types))

    print("[5] gene-key seed DOES emit self-typed link between TWO records with the same gene")
    two_var = {
        "report_metadata": {},
        "Genomic_Variant_umbrella": {"count_of_Genomic_Variants": 2, "llm_confidence_score": None,
                                     "Genomic_Variants": [
                                         {"gene_studied": "JAK2"},
                                         {"gene_studied": "JAK2"}]},
        "other_molecular_biomarker_umbrella": {"count_of_other_molecular_biomarkers": 0,
                                               "llm_confidence_score": None,
                                               "other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"count_of_tested_biomarkers": 0, "page_numbers": [],
                                      "llm_confidence_score": None, "tested_biomarkers": []},
    }
    res = Linker(link_registry=reg).link(sections=two_var, blocks=[], block_profiles=[])
    vsb = [(L.from_ref, L.to_ref) for L in res.links if L.type == "variant_superseded_by"]
    check("variant_superseded_by emitted ONLY between distinct records",
          len(vsb) > 0 and all(f != t for f, t in vsb), str(vsb))

    print("[6] Extractor sub-steps have distinct plain-language strings")
    text_root = _plain_agent({"agent": "Extractor"})
    text_th = _plain_agent({"agent": "Extractor · thought #3"})
    text_tool = _plain_agent({"agent": "Extractor · tool · hgnc_normalize"})
    text_res = _plain_agent({"agent": "Extractor · hgnc_normalize → result"})
    text_re = _plain_agent({"agent": "Extractor (re-extract)"})
    distinct = len({text_root, text_th, text_tool, text_res, text_re}) == 5
    check("five Extractor variants produce five distinct plain-language strings", distinct,
          str([text_root[:40], text_th[:40], text_tool[:40], text_res[:40], text_re[:40]]))
    check("thought variant mentions 'think' or 'pause'",
          "think" in text_th.lower() or "pause" in text_th.lower(), text_th)
    check("tool variant names the tool",
          "hgnc_normalize" in text_tool, text_tool)
    check("tool-result variant names the tool", "hgnc_normalize" in text_res, text_res)

    print("-" * 60)
    if fails:
        print(f"V4-M15 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M15 VERIFY: PASS — V3 orphan multi-section, no self-loop, per-step plain text.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
