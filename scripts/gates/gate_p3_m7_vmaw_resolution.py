"""
Phase 3 M7 gate — VMAW deep-resolution agent (EC / CITE / VA).

Fully offline (stub capability hooks; stub deps for the graph compile). Checks:
  [1] grounding gate: value present in a cited block → ok; absent / no block → fail.
  [2] grounded CONFIRMATION (needs_review + CITE, value in block) → auto_applied;
      the needs_review flag is cleared + a citation attached; occurrences intact.
  [3] contested VA pick → proposed_for_sme (NEVER auto-shipped); envelope untouched;
      item stays in queue with a vmaw_proposal.
  [4] ungrounded resolution (value not in cited block) → proposed_for_sme, not applied.
  [5] no LLM hooks → unresolved; item stays in queue (degrade to SME).
  [6] resolve() batch: auto items leave the queue, proposed/unresolved stay; vmaw_log written.
  [7] graph_selfcorrecting wires vmaw on the triage 'done' branch (triage→vmaw→decision_router).

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m7_vmaw_resolution.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace as NS

import yaml

from agents.link_binding_verifier import LinkBindingVerifier
from agents.linker import Linker
from agents.planner import Planner
from core.schema_loader import SchemaLoader
from decision.decision_router import DecisionRouter
from pipeline.graph_selfcorrecting import build_selfcorrecting_graph
from pipeline.vmaw import VMAWAgent

_BLOCKS = [{"block_id": "b1", "text": "Final staging: pT2 pN1 M0 per synoptic report."},
           {"block_id": "b2", "text": "HER2 IHC equivocal; EGFR amplified by FISH."}]


def _nr_envelope():
    """A needs_review inferred-token record + a biomarker carrying occurrences."""
    return {
        "significant_findings": {"specimen_findings": [
            {"specimen_id": "A", "pTNM_staging_details": {
                "pTNM_stage": "pT2", "needs_review": True, "review_reason": "OCR inferred pT2"}}]},
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
            {"biomarker_name": "HER2", "findings": [
                {"method": "IHC", "result": "equivocal",
                 "occurrences": [{"block_id": "b2", "surface": "HER2 IHC equivocal"}]}]}]},
    }


_NR_ITEM = {"kind": "needs_review",
            "ref": "significant_findings.specimen_findings[0].pTNM_staging_details",
            "detail": "OCR inferred pT2"}
_REFUTED_ITEM = {"kind": "binding_refuted",
                 "ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]",
                 "detail": "EGFR/HER2 mis-bind"}


def _cite_grounded(**kw):
    return {"value": "pT2", "block_ids": ["b1"], "span": "pT2 pN1 M0",
            "confidence": 0.95, "rationale": "synoptic report states pT2"}


def _cite_ungrounded(**kw):
    return {"value": "pT4", "block_ids": ["b1"], "span": "pT4",
            "confidence": 0.95, "rationale": "claims pT4 (not in cited block)"}


def _va_contested(**kw):
    return {"decision_value": "HER2 negative", "block_ids": ["b2"], "contested": True,
            "confidence": 0.8, "rationale": "two plausible bindings"}


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] grounding gate")
    check("value in cited block → ok", VMAWAgent._grounding_ok("pT2", ["b1"], _BLOCKS) is True)
    check("value absent from cited block → fail", VMAWAgent._grounding_ok("pT9", ["b1"], _BLOCKS) is False)
    check("no block ids → fail", VMAWAgent._grounding_ok("pT2", [], _BLOCKS) is False)
    check("unknown block id → fail", VMAWAgent._grounding_ok("pT2", ["bX"], _BLOCKS) is False)

    print("[2] grounded confirmation → auto_applied (flag cleared, occurrences intact)")
    env = _nr_envelope()
    agent = VMAWAgent(cite_fn=_cite_grounded)
    res = agent.resolve_item(_NR_ITEM, env, _BLOCKS)
    check("status auto_applied", res.status == "auto_applied", res.status)
    check("capability cite", res.capability == "cite", res.capability)
    out = agent.resolve({"escalation_queue": [_NR_ITEM], "extraction": env,
                         "doc_profile": {"blocks": _BLOCKS}})
    rec = out["extraction"]["significant_findings"]["specimen_findings"][0]["pTNM_staging_details"]
    check("needs_review cleared", rec.get("needs_review") is False, str(rec.get("needs_review")))
    check("citation attached", "vmaw_confirmed" in rec)
    check("auto item removed from SME queue", out["escalation_queue"] == [], str(out["escalation_queue"]))
    occ = out["extraction"]["other_molecular_biomarker_umbrella"]["other_molecular_biomarkers"][0]["findings"][0]["occurrences"]
    check("verbatim occurrences preserved", occ == [{"block_id": "b2", "surface": "HER2 IHC equivocal"}], str(occ))

    print("[3] contested VA → proposed_for_sme (never auto-shipped)")
    env2 = _nr_envelope()
    agent_va = VMAWAgent(adjudicate_value_fn=_va_contested)
    res2 = agent_va.resolve_item(_REFUTED_ITEM, env2, _BLOCKS)
    check("status proposed_for_sme", res2.status == "proposed_for_sme", res2.status)
    out2 = agent_va.resolve({"escalation_queue": [_REFUTED_ITEM], "extraction": env2,
                             "doc_profile": {"blocks": _BLOCKS}})
    check("contested item kept in queue", len(out2["escalation_queue"]) == 1)
    check("queue item carries vmaw_proposal", "vmaw_proposal" in out2["escalation_queue"][0])
    bm = out2["extraction"]["other_molecular_biomarker_umbrella"]["other_molecular_biomarkers"][0]
    check("envelope NOT mutated by contested pick", bm["findings"][0]["result"] == "equivocal")

    print("[4] ungrounded resolution → proposed_for_sme, not applied")
    env3 = _nr_envelope()
    agent_ung = VMAWAgent(cite_fn=_cite_ungrounded)
    res3 = agent_ung.resolve_item(_NR_ITEM, env3, _BLOCKS)
    check("status proposed_for_sme (ungrounded)", res3.status == "proposed_for_sme", res3.status)
    check("grounded flag False", res3.grounded is False)
    out3 = agent_ung.resolve({"escalation_queue": [_NR_ITEM], "extraction": env3,
                              "doc_profile": {"blocks": _BLOCKS}})
    rec3 = out3["extraction"]["significant_findings"]["specimen_findings"][0]["pTNM_staging_details"]
    check("needs_review NOT cleared on ungrounded", rec3.get("needs_review") is True)

    print("[5] no hooks → unresolved, stays in queue")
    env4 = _nr_envelope()
    out4 = VMAWAgent().resolve({"escalation_queue": [_NR_ITEM], "extraction": env4,
                                "doc_profile": {"blocks": _BLOCKS}})
    check("item retained", len(out4["escalation_queue"]) == 1)
    check("annotated unresolved", out4["escalation_queue"][0].get("vmaw_note", {}).get("status") == "unresolved")

    print("[6] batch: auto leaves queue, contested stays, vmaw_log written")
    env5 = _nr_envelope()
    agent_both = VMAWAgent(cite_fn=_cite_grounded, adjudicate_value_fn=_va_contested)
    out5 = agent_both.resolve({"escalation_queue": [_NR_ITEM, _REFUTED_ITEM], "extraction": env5,
                               "doc_profile": {"blocks": _BLOCKS}})
    kinds_left = [i.get("kind") for i in out5["escalation_queue"]]
    check("only the contested item remains", kinds_left == ["binding_refuted"], str(kinds_left))
    check("vmaw_log has both items", len(out5["vmaw_log"]) == 2)
    check("vmaw_resolutions has both", len(out5["vmaw_resolutions"]) == 2)

    print("[8] M10c: attribution_contested — CITE grounds → confirm (value NOT overwritten); VA → SME")
    attr_blocks = [{"block_id": "b3", "text": "Part A: right breast, lumpectomy. Tissue type: Breast."}]

    def _attr_env():
        return {"significant_findings": {"specimen_findings": [{"specimen": [
            {"specimen_id": "A", "tissue_type": "Breast", "laterality": "Right",
             "occurrences": [{"block_id": "b3", "surface": "Part A: right breast"}]}]}]}}

    attr_item = {"kind": "attribution_contested",
                 "ref": "significant_findings.specimen_findings[0].specimen[0].tissue_type",
                 "detail": "tissue_type may not describe specimen A"}

    def _cite_attr(**kw):
        return {"value": "Breast", "block_ids": ["b3"], "span": "Tissue type: Breast",
                "confidence": 0.95, "rationale": "Part A is breast"}

    enva = _attr_env()
    out_a = VMAWAgent(cite_fn=_cite_attr).resolve(
        {"escalation_queue": [attr_item], "extraction": enva, "doc_profile": {"blocks": attr_blocks}})
    sp = out_a["extraction"]["significant_findings"]["specimen_findings"][0]["specimen"][0]
    check("attribution CITE-grounded → auto_applied (item leaves queue)", out_a["escalation_queue"] == [])
    check("value NOT overwritten (tissue_type still 'Breast')", sp.get("tissue_type") == "Breast")
    check("attribution confirmation attached to owner object",
          "tissue_type" in (sp.get("vmaw_attribution_confirmed") or {}), str(sp.get("vmaw_attribution_confirmed")))
    check("occurrences intact after attribution confirm",
          sp.get("occurrences") == [{"block_id": "b3", "surface": "Part A: right breast"}])

    def _va_attr(**kw):
        return {"decision_value": "specimen B", "block_ids": ["b3"], "contested": True,
                "confidence": 0.8, "rationale": "could be specimen A or B"}
    envb = _attr_env()
    res_va = VMAWAgent(adjudicate_value_fn=_va_attr).resolve_item(attr_item, envb, attr_blocks)
    check("contested VA attribution → proposed_for_sme", res_va.status == "proposed_for_sme", res_va.status)
    spb = envb["significant_findings"]["specimen_findings"][0]["specimen"][0]
    check("contested attribution does NOT mutate the envelope",
          spb.get("tissue_type") == "Breast" and "vmaw_attribution_confirmed" not in spb)

    print("[9] gap #5: VMAW drops an ungroundable record it can't resolve (after re-extract)")
    drop_blocks = [{"block_id": "b9", "text": "unrelated text, no support here"}]
    drop_env = {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
        {"biomarker_name": "JUNK", "findings": [
            {"result": "Detected", "occurrences": [{"block_id": "b9", "surface": "JUNK"}]}]}]}}
    drop_item = {"kind": "binding_refuted",
                 "ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]",
                 "detail": "bind grounds to no block"}
    # NO hooks → VMAW can't ground → unresolved → droppable kind → drop the record
    out_d = VMAWAgent().resolve({"escalation_queue": [drop_item], "extraction": drop_env,
                                 "doc_profile": {"blocks": drop_blocks}})
    check("ungroundable record removed from envelope",
          out_d["extraction"]["other_molecular_biomarker_umbrella"]["other_molecular_biomarkers"] == [])
    q0 = out_d["escalation_queue"][0]
    check("queue item re-kinded dropped_ungroundable", q0.get("kind") == "dropped_ungroundable")
    check("dropped payload preserved for SME audit/restore", q0.get("dropped_record", {}).get("biomarker_name") == "JUNK")
    # a NON-droppable unresolved kind (needs_review) is NOT dropped — stays flagged
    nr_out = VMAWAgent().resolve({"escalation_queue": [_NR_ITEM], "extraction": _nr_envelope(),
                                  "doc_profile": {"blocks": _BLOCKS}})
    rec_nr = nr_out["extraction"]["significant_findings"]["specimen_findings"][0]
    check("needs_review NOT dropped (record intact)", bool(rec_nr) and len(nr_out["escalation_queue"]) == 1)

    print("[7] graph_selfcorrecting wires vmaw on the triage 'done' branch")
    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")
    teams_cfg = yaml.safe_load(open("config/teams.yaml", encoding="utf-8"))
    stub_deps = NS(storage_config=None, schema_loader=sl, prompt_renderer=None, persistence=NS(),
                   preprocess_deps=NS(), planner=Planner(), teams={}, linker=Linker(),
                   binding_verifier=LinkBindingVerifier(), decision_router=DecisionRouter(),
                   coverage_gap_tolerance=0.10)
    graph = build_selfcorrecting_graph(stub_deps, teams_cfg=teams_cfg, checkpointer=None)
    nodes = set(graph.get_graph().nodes)
    edges = [(e.source, e.target) for e in graph.get_graph().edges]
    check("vmaw node wired", "vmaw" in nodes, str(sorted(nodes)))
    check("triage → vmaw (done branch)", ("triage", "vmaw") in edges, str(edges))
    check("vmaw → decision_router", ("vmaw", "decision_router") in edges)
    check("triage no longer goes straight to router", ("triage", "decision_router") not in edges)

    print("[8] vmaw_refuted: VMAW investigates a link_cannot_form, returns ungrounded +")
    print("    uncontested + rationale  →  link is DROPPED (not proposed_for_sme)")
    # Mirrors the live failure mode from the SME queue (TP53↔MSI, STK11↔LZTR1, STK11↔NTRK).
    # VA returns a refutation rationale with no grounding evidence — that IS the refutation,
    # so the link should be removed from the envelope, not surfaced to SME under 'judgment'.
    def _va_refute(*args, **kwargs):
        return {"value": True, "block_ids": [], "rationale":
                "Source explicitly lists them separately with their own results.",
                "confidence": 0.9, "contested": False}
    refute_env = {"links": [{
        "from_ref": "Genomic_Variant_umbrella.Genomic_Variants[0]",
        "to_ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]",
        "type": "tested_to_result"}]}
    refute_item = {"kind": "link_cannot_form", "ref": "links[0]",
                   "section": "links", "detail": "TP53 / MSI"}
    out_ref = VMAWAgent(adjudicate_value_fn=_va_refute).resolve({
        "extraction": refute_env, "escalation_queue": [refute_item],
        "doc_profile": {"blocks": []}})
    new_q = out_ref["escalation_queue"]
    check("link_cannot_form removed from queue under that kind",
          not any(it.get("kind") == "link_cannot_form" for it in new_q), str(new_q))
    check("dropped item carries vmaw_refuted note (not 'dropped')",
          any((it.get("vmaw_note") or {}).get("status") == "vmaw_refuted" for it in new_q))
    check("dropped item carries the VMAW rationale for SME audit",
          any("separately" in str((it.get("vmaw_note") or {}).get("rationale") or "")
              for it in new_q))
    check("link removed from envelope",
          not out_ref["extraction"].get("links"), str(out_ref["extraction"].get("links")))

    print("[9] S3 — VMAW confirmation auto-accept (binding_refuted where VMAW's")
    print("    grounded value MATCHES the original extraction → no SME, no review_light)")
    # The MSI case from full_report_Redacted: extractor wrote 'MSI' / 'Not Detected'
    # citing block 52; V3 hallucination check refuted because 'msi' isn't in source
    # text literally; VMAW's VA grounded "Not Detected" at block 52 (same answer).
    # Pre-S3 this surfaced as review_light (queue, status=proposed_for_sme).
    # Post-S3 it auto-applies → item LEAVES the queue entirely.
    def _va_confirm(*args, **kwargs):
        return {"value": "Not Detected", "block_ids": ["52"],
                "rationale": "block 52 says MICROSATELLITE INSTABILITY: Not Detected",
                "confidence": 0.95, "contested": False}
    confirm_env = {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
        {"biomarker_name": "MSI", "result": "Not Detected", "method": "PCR"}]}}
    confirm_blocks = [{"block_id": "52", "text": "MICROSATELLITE INSTABILITY: Not Detected"}]
    confirm_item = {"kind": "binding_refuted",
                    "ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]",
                    "section": "other_molecular_biomarker_umbrella",
                    "detail": "biomarker 'msi' not present in any source block"}
    out_conf = VMAWAgent(adjudicate_value_fn=_va_confirm).resolve({
        "extraction": confirm_env, "escalation_queue": [confirm_item],
        "doc_profile": {"blocks": confirm_blocks}})
    check("S3 confirm: item left the queue (no SME workload)",
          out_conf["escalation_queue"] == [],
          str(out_conf["escalation_queue"]))
    # vmaw_resolutions carries the auto_applied audit record.
    autos = [r for r in (out_conf.get("vmaw_resolutions") or [])
             if r.get("status") == "auto_applied"]
    check("S3 confirm: vmaw_resolutions has exactly one auto_applied record",
          len(autos) == 1,
          str(out_conf.get("vmaw_resolutions")))
    check("S3 confirm: rationale includes 'vmaw_confirmation' (audit trail)",
          autos and "vmaw_confirmation" in str(autos[0].get("rationale") or ""),
          str(autos[0].get("rationale") if autos else None))

    print("[10] S3 — VMAW *pick* (different value) STILL routes to SME (T17 preserved)")
    # When VMAW's value DIFFERS from what's already in the envelope, this is a
    # genuine adjudication — the conservative T17 rule must still fire.
    def _va_pick(*args, **kwargs):
        return {"value": "Detected", "block_ids": ["52"],          # ← DIFFERENT
                "rationale": "block 52 actually says Detected", "confidence": 0.92,
                "contested": False}
    out_pick = VMAWAgent(adjudicate_value_fn=_va_pick).resolve({
        "extraction": confirm_env, "escalation_queue": [confirm_item],
        "doc_profile": {"blocks": confirm_blocks}})
    pick_resolutions = [r for r in (out_pick.get("vmaw_resolutions") or [])
                        if r.get("status") == "proposed_for_sme"]
    check("S3 pick: vmaw_resolutions records proposed_for_sme (T17 fires)",
          len(pick_resolutions) == 1,
          str(out_pick.get("vmaw_resolutions")))
    check("S3 pick: item REMAINS in queue with vmaw_proposal for SME",
          len(out_pick["escalation_queue"]) == 1 and
          out_pick["escalation_queue"][0].get("vmaw_proposal") is not None,
          str(out_pick["escalation_queue"]))

    print("-" * 60)
    if fails:
        print(f"P3-M7 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M7 VERIFY: PASS — VMAW grounds & auto-applies confirmations, holds contested picks for SME.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
