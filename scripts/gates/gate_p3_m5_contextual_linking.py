"""
Phase 3 M5 gate — contextual linking over the typed registry (Tier 1+2).

No LLM/cloud — the link adjudicator is a stub. Checks:
  [1] registry loads; schema-completeness clean; a bad endpoint is caught.
  [2] catalog_text lists active Tier 1+2 types, NOT the inactive Tier 3 ones.
  [3] validate_link drops unknown-type / above-tier / dangling-ref /
      endpoint-mismatch / no-evidence / unknown-evidence-block / low-confidence,
      and keeps a fully-valid link.
  [4] Linker integration: a stub adjudicator returning a MIX of good+bad links
      commits only the valid one (method="contextual").
  [5] deterministic-seed TOGGLE: seed ON → variant_on_panel committed; seed OFF →
      not committed but passed to the adjudicator as seed_hints (+ catalog fed).
  [6] graceful fallback: a legacy-signature adjudicator (no M5 kwargs) still runs.
  [7] back-compat: no registry → legacy resolve-both-refs validation.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m5_contextual_linking.py
"""

from __future__ import annotations

import sys
import tempfile

from agents.link_registry import LinkRegistry, validate_against_schema
from agents.linker import Linker
from core.schema_loader import SchemaLoader

REG = "config/link_registry.yaml"
_FAKE_HGNC = lambda s: {"canonical": (s or "").upper()}  # noqa: E731 — offline normalizer


def _bm_panel_sections():
    """JAK2 sequence-variant biomarker + JAK2 panel entry + one specimen."""
    return {
        "other_molecular_biomarker_umbrella": {
            "count": 1, "llm_confidence_score": 0.9,
            "other_molecular_biomarkers": [
                {"biomarker_name": "JAK2", "findings": [
                    {"method": "PCR", "result": "Detected",
                     "variant_detail": {"amino_acid_change": "p.V617F"},
                     "occurrences": [{"block_id": "b1", "surface": "JAK2"}]}]}]},
        "tested_biomarker_umbrella": {
            "count_of_tested_biomarkers": 1, "page_numbers": [1],
            "llm_confidence_score": 0.9, "tested_biomarkers": ["JAK2"]},
        "significant_findings": {
            "count_of_specimen_findings": 1, "llm_confidence_score": 0.9,
            "specimen_findings": [{"specimen": [{"specimen_id": "A"}]}]},
    }


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")
    reg = LinkRegistry.from_path(REG, max_tier=2)

    print("[1] registry loads + schema-completeness")
    check("real registry references only real schema sections", validate_against_schema(REG, sl) == [],
          str(validate_against_schema(REG, sl)))
    bad = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    bad.write("link_types:\n  - {type: x, tier: 1, from_section: NOPE, to_section: significant_findings}\n")
    bad.flush()
    check("bad endpoint section caught", len(validate_against_schema(bad.name, sl)) >= 1)

    print("[2] catalog_text lists Tier 1+2, not inactive Tier 3")
    cat = reg.catalog_text()
    check("catalog has tested_to_result (T1)", "tested_to_result" in cat)
    check("catalog has finding_interpretation (T2)", "finding_interpretation" in cat)
    check("catalog excludes inactive indication_for (T3)", "indication_for" not in cat)
    check("is_emittable: active T1 yes", reg.is_emittable("biomarker_on_specimen"))
    check("is_emittable: inactive T3 no", not reg.is_emittable("indication_for"))

    print("[3] validate_link drop/keep matrix")
    env = {
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [{"biomarker_name": "JAK2"}]},
        "significant_findings": {"specimen_findings": [{"specimen": [{"specimen_id": "A"}]}]},
    }
    blocks = [{"block_id": "b1", "text": "JAK2 detected in specimen A"}]
    bm0 = "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]"
    sp0 = "significant_findings.specimen_findings[0]"

    def vl(**over):
        base = {"from_ref": bm0, "to_ref": sp0, "type": "biomarker_on_specimen",
                "evidence_block_ids": ["b1"], "confidence": 0.9}
        base.update(over)
        return reg.validate_link(link=base, envelope=env, blocks=blocks, min_confidence=0.5)

    check("valid link kept", vl()[0] is True, vl()[1])
    check("unknown type dropped", vl(type="totally_made_up")[0] is False)
    check("inactive/above-tier type dropped", vl(type="indication_for")[0] is False)
    check("dangling ref dropped", vl(to_ref="significant_findings.specimen_findings[9]")[0] is False)
    check("endpoint mismatch dropped", vl(type="staging_of_specimen")[0] is False)  # needs sf↔sf
    check("no-evidence dropped", vl(evidence_block_ids=[])[0] is False)
    check("unknown evidence block dropped", vl(evidence_block_ids=["bZ"])[0] is False)
    check("low confidence dropped", vl(confidence=0.1)[0] is False)

    print("[4] Linker integration: mix of good+bad → only valid committed")
    proposed = [
        {"from_ref": bm0, "to_ref": sp0, "type": "biomarker_on_specimen",
         "rationale": "JAK2 assayed on specimen A", "evidence_block_ids": ["b1"], "confidence": 0.9},  # valid
        {"from_ref": bm0, "to_ref": sp0, "type": "made_up", "rationale": "",
         "evidence_block_ids": ["b1"], "confidence": 0.9},                                            # unknown type
        {"from_ref": bm0, "to_ref": "significant_findings.specimen_findings[9]", "type": "biomarker_on_specimen",
         "rationale": "", "evidence_block_ids": ["b1"], "confidence": 0.9},                           # dangling
        {"from_ref": bm0, "to_ref": sp0, "type": "biomarker_on_specimen", "rationale": "",
         "evidence_block_ids": [], "confidence": 0.9},                                                # no evidence
    ]
    lk = Linker(hgnc_normalize=_FAKE_HGNC, link_registry=reg,
                link_adjudicator=lambda **kw: proposed)
    res = lk.link(sections=_bm_panel_sections(), blocks=blocks)
    ctx = [l for l in res.links if l.method == "contextual"]
    check("exactly one contextual link committed", len(ctx) == 1, str([(l.type) for l in ctx]))
    check("the committed contextual link is biomarker_on_specimen",
          ctx and ctx[0].type == "biomarker_on_specimen")

    print("[5] deterministic-seed toggle")
    # seed ON (default) → variant_on_panel committed deterministically
    lk_on = Linker(hgnc_normalize=_FAKE_HGNC, link_registry=reg, link_seed_enabled=True)
    res_on = lk_on.link(sections=_bm_panel_sections(), blocks=blocks)
    check("seed ON → variant_on_panel committed",
          any(l.type == "variant_on_panel" and l.method == "deterministic" for l in res_on.links))
    # seed OFF → NOT committed, but passed as seed_hints (+ catalog) to adjudicator
    captured: dict = {}
    def capturing_adj(**kw):
        captured.update(kw)
        return []
    lk_off = Linker(hgnc_normalize=_FAKE_HGNC, link_registry=reg,
                    link_seed_enabled=False, link_adjudicator=capturing_adj)
    res_off = lk_off.link(sections=_bm_panel_sections(), blocks=blocks)
    check("seed OFF → variant_on_panel NOT committed",
          not any(l.type == "variant_on_panel" for l in res_off.links))
    check("seed OFF → seed hint passed to adjudicator",
          any(h.get("type") == "variant_on_panel" for h in (captured.get("seed_hints") or [])),
          str(captured.get("seed_hints")))
    check("catalog fed to adjudicator", bool(captured.get("link_catalog")))

    print("[6] graceful fallback for a legacy-signature adjudicator")
    def legacy_adj(*, envelope, blocks=None):  # no link_catalog / seed_hints kwargs
        return []
    lk_legacy = Linker(hgnc_normalize=_FAKE_HGNC, link_registry=reg, link_adjudicator=legacy_adj)
    try:
        lk_legacy.link(sections=_bm_panel_sections(), blocks=blocks)
        check("legacy-signature adjudicator runs without error", True)
    except Exception as exc:  # noqa: BLE001
        check("legacy-signature adjudicator runs without error", False, str(exc))

    print("[7] back-compat: no registry → legacy resolve-both-refs validation")
    legacy_proposed = [
        {"from_ref": bm0, "to_ref": sp0, "type": "anything_goes_in_legacy",
         "rationale": "", "evidence_block_ids": [], "confidence": 0.1},   # resolves → kept in legacy
        {"from_ref": bm0, "to_ref": "significant_findings.specimen_findings[9]",
         "type": "x", "rationale": "", "evidence_block_ids": [], "confidence": 0.9},  # dangling → dropped
    ]
    lk_nor = Linker(hgnc_normalize=_FAKE_HGNC, link_registry=None,
                    link_adjudicator=lambda **kw: legacy_proposed)
    res_nor = lk_nor.link(sections=_bm_panel_sections(), blocks=blocks)
    ctx_nor = [l for l in res_nor.links if l.method == "contextual"]
    check("legacy mode keeps the resolving link regardless of type/evidence", len(ctx_nor) == 1,
          str([(l.type) for l in ctx_nor]))

    print("[8] M10a: intra-finding describes_specimen family + procedure registered")
    new_types = ["procedure_of_specimen", "gross_describes_specimen", "microscopic_describes_specimen",
                 "morphology_of_specimen", "histologic_describes_specimen", "ancillary_describes_specimen"]
    for t in new_types:
        lt = reg.get(t)
        check(f"{t} registered, Tier 2, active, sf↔sf",
              lt is not None and lt.tier == 2 and lt.active
              and lt.endpoints == frozenset({"significant_findings"}), str(lt))
        check(f"{t} emittable + in catalog", reg.is_emittable(t) and t in cat)
    check("registry still schema-complete with the new rows", validate_against_schema(REG, sl) == [])
    # Tier-3 clinical links remain a config toggle: present but inactive by default.
    for t in ("indication_for", "clinical_history_supports_finding"):
        check(f"{t} present but inactive (toggle off)", reg.get(t) is not None and not reg.is_emittable(t))

    print("[9] M10b: deep (nested) refs resolve for intra-finding links")
    deep_env = {
        "significant_findings": {"specimen_findings": [
            {"specimen": [{"specimen_id": "A", "tissue_type": "Breast"}],
             "gross_description": {"text": "1.2 cm tumor, tan-white"}}]}}
    g_from = "significant_findings.specimen_findings[0].gross_description"
    g_to = "significant_findings.specimen_findings[0].specimen[0]"

    def vld(**over):
        base = {"from_ref": g_from, "to_ref": g_to, "type": "gross_describes_specimen",
                "evidence_block_ids": ["b1"], "confidence": 0.9}
        base.update(over)
        return reg.validate_link(link=base, envelope=deep_env, blocks=blocks, min_confidence=0.5)

    check("deep describes_specimen link kept (both deep refs resolve)", vld()[0] is True, vld()[1])
    check("deep ref to non-existent specimen index dropped",
          vld(to_ref="significant_findings.specimen_findings[0].specimen[5]")[0] is False)
    check("deep ref to non-existent sub-path dropped",
          vld(from_ref="significant_findings.specimen_findings[0].microscopic_description")[0] is False)
    check("deep endpoint sections still resolve to significant_findings (sf↔sf)",
          vld()[0] is True)
    # shallow-ref regression: the original biomarker_on_specimen keep must still hold
    check("shallow refs unaffected (biomarker_on_specimen still kept)", vl()[0] is True, vl()[1])

    print("[10] gap #3: relink_hints flow to the adjudicator as avoid_hints")
    captured2: dict = {}
    def capturing_adj2(**kw):
        captured2.update(kw)
        return []
    lk_relink = Linker(hgnc_normalize=_FAKE_HGNC, link_registry=reg, link_adjudicator=capturing_adj2)
    lk_relink.link(sections=_bm_panel_sections(), blocks=blocks,
                   relink_hints=[{"ref": bm0, "reason": "binding refuted the link"}])
    check("avoid_hints fed to adjudicator from relink_hints",
          any(h.get("ref") == bm0 for h in (captured2.get("avoid_hints") or [])),
          str(captured2.get("avoid_hints")))
    # legacy adjudicator (no avoid_hints kwarg) still runs via the 3-tier fallback
    lk_relink_legacy = Linker(hgnc_normalize=_FAKE_HGNC, link_registry=reg,
                              link_adjudicator=lambda *, envelope, blocks=None: [])
    try:
        lk_relink_legacy.link(sections=_bm_panel_sections(), blocks=blocks,
                              relink_hints=[{"ref": bm0, "reason": "x"}])
        check("legacy adjudicator still runs with relink_hints (fallback)", True)
    except Exception as exc:  # noqa: BLE001
        check("legacy adjudicator still runs with relink_hints (fallback)", False, str(exc))

    print("-" * 60)
    if fails:
        print(f"P3-M5 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M5 VERIFY: PASS — typed registry validates contextual links; seed toggle works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
