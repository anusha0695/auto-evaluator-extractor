"""
Phase 3 M6 gate — the scoped ping-back / repair loop (graph_selfcorrecting).

Fully offline (no LLM/cloud): stub teams for the repair executor, stub deps for
the graph compile. Checks:
  [1] build_defects maps each signal to the right typed defect + team + signature.
  [2] TriageAgent: addressable→repair request; escalate-only→escalation;
      true-absence recall miss is NOT a defect.
  [3] termination guards: recur-guard escalates a repeated signature; per-team cap
      and global cap escalate once reached.
  [4] RepairExecutor: re_extract updates section_outputs/team_results + writes a
      repair_log entry + bumps budget + clears repair_requests; drop_and_flag
      removes the record and queues it.
  [5] graph_selfcorrecting compiles and wires triage + repair + the conditional loop edge.
  [6] a simulated cycle: triage(repair) → repair(stub team) → triage(done).

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m6_triage_repair_loop.py
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace as NS

import yaml

from agents.link_binding_verifier import LinkBindingVerifier
from agents.linker import Linker
from agents.planner import Planner
from core.schema_loader import SchemaLoader
from decision.decision_router import DecisionRouter
from pipeline.graph_selfcorrecting import GRAPH_RECURSION_LIMIT, build_selfcorrecting_graph
from pipeline.repair import RepairExecutor
from pipeline.triage import TriageAgent, build_defects, triage_route


def _state_with_defects():
    return {
        "doc_id": "t", "active_team_keys": ["specimen_findings_team", "molecular_biomarker_team"],
        "verifier_scorecards": [
            {"verifier_name": "schema_validator", "passed": False,
             "field_errors": [{"loc": ["significant_findings", "specimen_findings", 0], "msg": "bad", "type": "type"}]},
            {"verifier_name": "recall_floor", "passed": True, "field_errors": [
                {"field_name": "significant_findings.pTNM_staging_details.pTNM_stage",
                 "role": "synoptic_report", "strictness": "loud", "status": "confirmed_miss", "block_id": "b9"},
                {"field_name": "clinical_information.history", "role": "clinical_history",
                 "strictness": "quiet", "status": "unconfirmed"}]},
        ],
        "binding_verifier": {"refuted": 1, "uncertain": 1},
        "extraction": {
            "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
                {"biomarker_name": "JAK2", "findings": [{"result": "Detected", "occurrences": []}]}]},
            "significant_findings": {"specimen_findings": [
                {"specimen_id": "A", "pTNM_staging_details": {"needs_review": True, "review_reason": "OCR"}}]},
        },
    }


class _StubTeam:
    """Async team double — returns a 'repaired' section output."""
    def __init__(self, section, out):
        self._section = section
        self._out = out
        self.calls = 0

    @property
    def schema_section(self):
        return self._section

    async def run(self, state, *, repair_hints=None):
        self.calls += 1
        return NS(schema_section=self._section, output=self._out,
                  verdict="auto_accept", needs_review_count=0)


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] build_defects maps signals → typed defects")
    defects = build_defects(_state_with_defects())
    by_type = {d.defect_type for d in defects}
    check("schema_error defect present", "schema_error" in by_type)
    check("recall_miss_present (loud confirmed) present", "recall_miss_present" in by_type)
    check("quiet recall miss NOT a defect",
          not any(d.target_ref == "clinical_information.history" for d in defects))
    check("missing_provenance present", "missing_provenance" in by_type)
    check("binding_refuted + binding_uncertain present",
          {"binding_refuted", "binding_uncertain"} <= by_type)
    check("needs_review present", "needs_review" in by_type)
    rm = next(d for d in defects if d.defect_type == "recall_miss_present")
    check("recall miss routed to specimen_findings_team", rm.team == "specimen_findings_team", rm.team)
    check("signature is (team|ref|type)",
          rm.signature == "specimen_findings_team|significant_findings.pTNM_staging_details.pTNM_stage|recall_miss_present",
          rm.signature)

    print("[1b] M10c: contested attribution → addressable (re-extract once → recur escalates)")
    attr_state = {
        "doc_id": "t", "active_team_keys": ["specimen_findings_team"],
        "verifier_scorecards": [{"verifier_name": "attribution", "passed": True, "field_errors": [
            {"target": "specimen_attributes",
             "ref": "significant_findings.specimen_findings[0].specimen[1].tissue_type",
             "owner_key": "specimen_id", "owner_id": "specimen[1]", "attribute": "tissue_type",
             "n_owners": 2, "status": "contested", "strictness": "loud", "block_id": "b2"},
            {"target": "specimen_attributes",
             "ref": "significant_findings.specimen_findings[0].specimen[0].laterality",
             "owner_key": "specimen_id", "owner_id": "A", "attribute": "laterality",
             "n_owners": 2, "status": "ungrounded", "strictness": "quiet", "block_id": "b1"}]}],
        "binding_verifier": {"refuted": 0, "uncertain": 0}, "extraction": {},
    }
    ad = build_defects(attr_state)
    ac = [d for d in ad if d.defect_type == "attribution_contested"]
    check("contested attribution → exactly one attribution_contested defect", len(ac) == 1,
          str([d.defect_type for d in ad]))
    check("ungrounded/quiet attribution is NOT escalated as a defect",
          all(d.defect_type == "attribution_contested" for d in ad))
    check("attribution defect routed to the owning team",
          ac and ac[0].team == "specimen_findings_team", ac[0].team if ac else "")
    dec_a = TriageAgent().decide(attr_state)
    areq = [r for r in dec_a.repair_requests if r["defect_type"] == "attribution_contested"]
    check("attribution_contested addressable → re_extract_team once",
          bool(areq) and areq[0]["action"] == "re_extract_team", str(areq))
    attr_recur = dict(attr_state, defect_signatures_seen=[ac[0].signature] if ac else [])
    dec_ar = TriageAgent().decide(attr_recur)
    check("recurred attribution contest escalates (→ VMAW/SME), not repaired",
          not any(r["defect_type"] == "attribution_contested" for r in dec_ar.repair_requests)
          and any(e["kind"] == "attribution_contested" for e in dec_ar.escalations))

    print("[1c] block-misroute → reprofile_block; item-level binding (V4→re_link)")
    gap_state = {
        "doc_id": "t", "active_team_keys": ["specimen_findings_team", "molecular_biomarker_team"],
        "doc_profile": {"block_profiles": [
            {"block_id": "bk", "target_umbrella_hints": ["clinical_information"]}]},  # routed AWAY
        "verifier_scorecards": [{"verifier_name": "recall_floor", "passed": True, "field_errors": [
            {"field_name": "significant_findings.pTNM_staging_details.pTNM_stage",
             "role": "synoptic_report", "strictness": "loud", "status": "confirmed_miss", "block_id": "bk"}]}],
        "binding_items": [
            {"ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0].findings[0]",
             "check": "V2", "verdict": "refuted", "evidence": "wrong test"},
            {"ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]",
             "check": "V4", "verdict": "refuted", "evidence": "link not supported"},
            {"ref": "significant_findings.specimen_findings[0]", "check": "V1", "verdict": "uncertain", "evidence": "y"}],
        "binding_verifier": {"refuted": 2, "uncertain": 1}, "extraction": {},
    }
    gd = build_defects(gap_state)
    gt = {d.defect_type for d in gd}
    check("recall miss whose block was routed away → block_misroute", "block_misroute" in gt, str(gt))
    check("V4 refuted → link_cannot_form (link defect)", "link_cannot_form" in gt)
    check("V2 refuted → item-level binding_refuted (targeted ref)",
          any(d.defect_type == "binding_refuted" and (d.target_ref or "").startswith("other_molecular") for d in gd))
    check("uncertain binding item → binding_uncertain", "binding_uncertain" in gt)
    gdec = TriageAgent().decide(gap_state)
    gacts = {r["defect_type"]: r["action"] for r in gdec.repair_requests}
    check("block_misroute → reprofile_block", gacts.get("block_misroute") == "reprofile_block", str(gacts))
    check("link_cannot_form → re_link (team-less action not escalated)", gacts.get("link_cannot_form") == "re_link")

    print("[1d] gap #6/#7: triage agent router — escalate-when-unfixable + team assignment")
    # (a) a schema_error whose loc section doesn't map to any team → team None
    unteam_state = {
        "doc_id": "t", "active_team_keys": ["molecular_biomarker_team", "specimen_findings_team"],
        "verifier_scorecards": [{"verifier_name": "schema_validator", "passed": False, "field_errors": [
            {"loc": ["mystery_section", "x"], "msg": "bad", "type": "type"}]}],
        "binding_verifier": {}, "extraction": {},
    }
    dec_no = TriageAgent().decide(unteam_state)
    check("no router → unteamable schema_error escalates",
          any("no targetable team" in e.get("reason", "") for e in dec_no.escalations))
    dec_team = TriageAgent(triage_llm=lambda **kw: {"action": "", "team": "molecular_biomarker_team",
                                                    "rationale": ""}).decide(unteam_state)
    se = [r for r in dec_team.repair_requests if r["defect_type"] == "schema_error"]
    check("router assigns team → schema_error becomes a repair request",
          bool(se) and se[0]["team"] == "molecular_biomarker_team", str(se))

    # (b) an ADDRESSABLE defect the agent judges re-extraction can't fix → straight to escalate (→VMAW)
    fixable_state = {
        "doc_id": "t", "active_team_keys": ["specimen_findings_team", "molecular_biomarker_team"],
        "verifier_scorecards": [{"verifier_name": "recall_floor", "passed": True, "field_errors": [
            {"field_name": "significant_findings.pTNM_staging_details.pTNM_stage",
             "role": "synoptic_report", "strictness": "loud", "status": "confirmed_miss", "block_id": "b9"}]}],
        "binding_verifier": {}, "extraction": {},
    }
    # deterministic default: recall_miss_present → re_extract_team (a real repair request)
    base = TriageAgent().decide(fixable_state)
    check("deterministic default repairs the recall miss",
          any(r["defect_type"] == "recall_miss_present" and r["action"] == "re_extract_team"
              for r in base.repair_requests))
    esc_router = lambda **kw: {"action": "escalate", "team": "",
                               "rationale": "value split across pages; needs context expansion"}
    dec_esc = TriageAgent(triage_llm=esc_router).decide(fixable_state)
    check("agent routes addressable defect straight to escalate (→VMAW), no re-extract",
          not any(r["defect_type"] == "recall_miss_present" for r in dec_esc.repair_requests)
          and any("agent:" in e.get("reason", "") for e in dec_esc.escalations), str(dec_esc.escalations))

    # (c) an INVALID action from the router is ignored → deterministic action stands
    dec_bad = TriageAgent(triage_llm=lambda **kw: {"action": "not_a_real_action"}).decide(fixable_state)
    check("invalid router action ignored → deterministic repair stands",
          any(r["defect_type"] == "recall_miss_present" and r["action"] == "re_extract_team"
              for r in dec_bad.repair_requests))

    print("[2] TriageAgent partitions addressable vs escalate")
    agent = TriageAgent()
    d = agent.decide(_state_with_defects())
    check("route is repair (addressable defects exist)", d.route == "repair", d.route)
    actions = {r["action"] for r in d.repair_requests}
    check("re_extract_team queued", "re_extract_team" in actions, str(actions))
    esc_kinds = {e["kind"] for e in d.escalations}
    check("binding_uncertain escalated", "binding_uncertain" in esc_kinds, str(esc_kinds))
    check("needs_review escalated", "needs_review" in esc_kinds)
    check("triage_route reads repair_requests",
          triage_route({"repair_requests": d.repair_requests}) == "repair"
          and triage_route({"repair_requests": []}) == "done")

    print("[3] termination guards")
    st = _state_with_defects()
    st["defect_signatures_seen"] = [rm.signature]               # recur-guard
    d2 = agent.decide(st)
    check("recurred signature escalated, not repaired",
          rm.signature not in {r["signature"] for r in d2.repair_requests}
          and any(e["ref"] == rm.target_ref and "recurred" in e["reason"] for e in d2.escalations))
    st_cap = _state_with_defects()
    st_cap["repair_log"] = [{"action": "re_extract_team", "team": "specimen_findings_team"}]
    d3 = agent.decide(st_cap)
    check("per-team cap reached → that team's recall miss escalates",
          not any(r["team"] == "specimen_findings_team" and r["defect_type"] == "recall_miss_present"
                  for r in d3.repair_requests))
    st_gc = _state_with_defects()
    st_gc["repair_budget_used"] = len(st_gc["active_team_keys"]) * 2   # global cap = teams×2
    d4 = agent.decide(st_gc)
    check("global budget exhausted → no repair requests", d4.repair_requests == [], str(d4.repair_requests))

    print("[4] RepairExecutor applies the catalog")
    repaired_out = {"other_molecular_biomarkers": [
        {"biomarker_name": "JAK2", "findings": [{"result": "Detected",
         "occurrences": [{"block_id": "b1", "surface": "JAK2"}]}]}], "llm_confidence_score": 0.9}
    stub = _StubTeam("other_molecular_biomarker_umbrella", repaired_out)
    ex = RepairExecutor(teams={"molecular_biomarker_team": stub})
    st_rep = {
        "section_outputs": {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": []}},
        "team_results": {}, "repair_log": [], "escalation_queue": [], "repair_budget_used": 0,
        "repair_requests": [{"action": "re_extract_team", "team": "molecular_biomarker_team",
                             "section": "other_molecular_biomarker_umbrella",
                             "target_ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]",
                             "defect_type": "missing_provenance", "detail": "no occurrences",
                             "candidate_block_ids": ["b1"], "signature": "sig1"}],
    }
    delta = asyncio.run(ex.apply(st_rep))
    check("stub team re-run", stub.calls == 1)
    check("section_outputs updated with repaired output",
          delta["section_outputs"]["other_molecular_biomarker_umbrella"] == repaired_out)
    check("repair_log entry written", len(delta["repair_log"]) == 1
          and delta["repair_log"][0]["outcome"] == "re_extracted")
    check("budget incremented", delta["repair_budget_used"] == 1)
    check("repair_requests cleared", delta["repair_requests"] == [])

    # drop_and_flag
    ex2 = RepairExecutor(teams={})
    st_drop = {"section_outputs": {"significant_findings": {"specimen_findings": [{"specimen_id": "A"}, {"specimen_id": "B"}]}},
               "team_results": {}, "repair_log": [], "escalation_queue": [], "repair_budget_used": 0,
               "repair_requests": [{"action": "drop_and_flag", "section": "significant_findings",
                                    "target_ref": "significant_findings.specimen_findings[0]",
                                    "defect_type": "ungroundable", "detail": "x", "signature": "s"}]}
    d_drop = asyncio.run(ex2.apply(st_drop))
    check("drop_and_flag removed the record",
          len(d_drop["section_outputs"]["significant_findings"]["specimen_findings"]) == 1
          and d_drop["section_outputs"]["significant_findings"]["specimen_findings"][0]["specimen_id"] == "B")
    check("dropped record queued to SME", any(e["kind"] == "dropped_ungroundable" for e in d_drop["escalation_queue"]))

    # reprofile_block: patches the block's hints + re-extracts the now-correct team
    stubR = _StubTeam("significant_findings", {"specimen_findings": [{"specimen_id": "A"}]})
    exR = RepairExecutor(teams={"specimen_findings_team": stubR})
    stR = {"section_outputs": {"significant_findings": {}}, "team_results": {}, "repair_log": [],
           "escalation_queue": [], "repair_budget_used": 0,
           "doc_profile": {"block_profiles": [{"block_id": "bk", "target_umbrella_hints": ["clinical_information"]}]},
           "repair_requests": [{"action": "reprofile_block", "team": "specimen_findings_team",
                                "section": "significant_findings", "target_ref": "significant_findings.x",
                                "defect_type": "block_misroute", "candidate_block_ids": ["bk"], "signature": "r"}]}
    dR = asyncio.run(exR.apply(stR))
    check("reprofile added the section to the block's hints",
          "significant_findings" in dR["doc_profile"]["block_profiles"][0]["target_umbrella_hints"])
    check("reprofile re-extracted the now-correct team", stubR.calls == 1)
    check("reprofile logged as reprofile_block", dR["repair_log"][0]["action"] == "reprofile_block")

    # re_link: team-less; the repair→linker edge does the actual re-link
    exL = RepairExecutor(teams={})
    dL = asyncio.run(exL.apply({"section_outputs": {}, "team_results": {}, "repair_log": [],
        "escalation_queue": [], "repair_budget_used": 0,
        "repair_requests": [{"action": "re_link", "section": "x", "target_ref": "x",
                             "defect_type": "link_cannot_form", "signature": "s"}]}))
    check("re_link logged as relink_pending (linker edge re-links)",
          dL["repair_log"][0]["outcome"] == "relink_pending")
    check("re_link accumulates an avoid/re-evaluate relink hint (gap #3)",
          dL.get("relink_hints") and dL["relink_hints"][0]["ref"] == "x", str(dL.get("relink_hints")))

    print("[5] graph_selfcorrecting compiles + wires the loop")
    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")
    teams_cfg = yaml.safe_load(open("config/teams.yaml", encoding="utf-8"))
    stub_deps = NS(
        storage_config=None, schema_loader=sl, prompt_renderer=None, persistence=NS(),
        preprocess_deps=NS(), planner=Planner(), teams={}, linker=Linker(),
        binding_verifier=LinkBindingVerifier(), decision_router=DecisionRouter(),
        coverage_gap_tolerance=0.10)
    try:
        graph = build_selfcorrecting_graph(stub_deps, teams_cfg=teams_cfg, checkpointer=None)
        nodes = set(graph.get_graph().nodes)
        expected = {"document_received", "preprocess", "planner", "teams", "linker",
                    "verifiers", "triage", "repair", "decision_router", "persist"}
        check("all v3 nodes wired (incl. triage + repair)", expected <= nodes, str(sorted(nodes)))
        edges = [(e.source, e.target) for e in graph.get_graph().edges]
        check("repair → linker edge (the cycle)", ("repair", "linker") in edges, str(edges))
        check("verifiers → triage edge", ("verifiers", "triage") in edges)
    except Exception as exc:  # noqa: BLE001
        check("build_selfcorrecting_graph compiles", False, str(exc))
    check("recursion_limit backstop defined", isinstance(GRAPH_RECURSION_LIMIT, int)
          and GRAPH_RECURSION_LIMIT > 0)

    print("[5b] v3 verifier persists UI-shaped verification_v2 (links + scorecards) — not just v3")
    from pipeline.graph_selfcorrecting import _make_selfcorrecting_verifier_node

    class _FP:
        def __init__(self):
            self.arts: dict = {}

        async def write_artifact(self, *, doc_id, kind, content):
            self.arts[kind] = content

    fp = _FP()
    vnode = _make_selfcorrecting_verifier_node(schema_loader=sl, binding_verifier=LinkBindingVerifier(),
                                   gap_tol=0.10, persistence=fp)
    asyncio.run(vnode({"doc_id": "t", "extraction": {},
                       "links": [{"type": "biomarker_on_specimen", "from_ref": "a", "to_ref": "b"}],
                       "doc_profile": {"blocks": []}, "parser_hypothesis": {}}))
    import json as _json
    check("v3 verifier writes verification_v2 (for the UI)", "verification_v2" in fp.arts, str(list(fp.arts)))
    _v2 = _json.loads(fp.arts.get("verification_v2", "{}"))
    check("verification_v2 carries links + scorecards", "links" in _v2 and "scorecards" in _v2
          and _v2["links"] and _v2["links"][0]["type"] == "biomarker_on_specimen")

    print("[5c] recall-floor AI re-read hook builds + degrades safely (keep miss)")
    from agents.adjudicators import build_recall_reread_fn
    rr = build_recall_reread_fn()
    check("reread is callable", callable(rr))
    # offline (no LLM / block not visible) → True = keep the miss (never silently drop)
    check("no block text → keep miss (True)",
          rr(block_id="bk", section="significant_findings", field_path=["pTNM_stage"], blocks=[]) is True)

    print("[6] simulated cycle: repair-then-clean terminates")
    st_cycle = _state_with_defects()
    dec1 = agent.decide(st_cycle)
    check("cycle: first triage routes to repair", dec1.route == "repair")
    # emulate repair having fixed everything: clean state, signatures recorded
    st_clean = {"doc_id": "t", "active_team_keys": st_cycle["active_team_keys"],
                "verifier_scorecards": [{"verifier_name": "schema_validator", "passed": True, "field_errors": []},
                                        {"verifier_name": "recall_floor", "passed": True, "field_errors": []}],
                "binding_verifier": {"refuted": 0, "uncertain": 0}, "extraction": {},
                "defect_signatures_seen": [r["signature"] for r in dec1.repair_requests]}
    dec2 = agent.decide(st_clean)
    check("cycle: second triage routes to done (no defects)", dec2.route == "done", dec2.route)

    print("[7] V3 orphan gate — only ESCALATE genuinely-illegible unattached candidates")
    from agents.link_binding_verifier import LinkBindingVerifier as _LBV
    env_o = {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
        {"biomarker_name": "JAK2",
         "findings": [{"result": "Detected", "variant_detail": {"amino_acid_change": "V617F"}}]}]}}
    tbi = {"b1": "JAK2 V617F Detected", "b2": "MRN 12345", "b3": "Q@#~|^ garble"}
    ph_o = {"candidates": [
        # attached: V617F lands in variant_detail → NOT an orphan (whole-record haystack)
        {"target_umbrella": "other_molecular_biomarker_umbrella", "text": "V617F",
         "occurrences": [{"block_id": "b1"}]},
        # clean non-biomarker token → dropped, NOT escalated
        {"target_umbrella": "other_molecular_biomarker_umbrella", "text": "MRN",
         "occurrences": [{"block_id": "b2"}]},
        # genuinely garbled source → escalate to SME
        {"target_umbrella": "other_molecular_biomarker_umbrella", "text": "Q@#~|^",
         "occurrences": [{"block_id": "b3"}]}]}
    vo = _LBV._v3_orphan_hallucination(env_o, tbi, ph_o)
    orph = {v.ref for v in vo if str(v.ref).startswith("candidate:")}
    check("attached variant (V617F) is NOT a false orphan", "candidate:V617F" not in orph, str(orph))
    check("clean non-biomarker token (MRN) is DROPPED, not escalated", "candidate:MRN" not in orph)
    check("genuinely-garbled candidate IS escalated", "candidate:Q@#~|^" in orph)
    check("legibility: clean date/code not jumbled",
          not _LBV._illegible("08/12/2025") and not _LBV._illegible("1849G>"))
    check("legibility: OCR splatter is jumbled", _LBV._illegible("J@K2 V6l7F~~|"))

    print("[8] SME queue dedup across verifier→triage passes")
    from pipeline.triage import make_triage_node
    node = make_triage_node()
    st_dup = {"doc_id": "t", "active_team_keys": ["molecular_biomarker_team"],
              "verifier_scorecards": [], "binding_verifier": {"refuted": 0, "uncertain": 0},
              "binding_items": [{"ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0].findings[0]",
                                 "check": "V1", "verdict": "uncertain", "evidence": "e"}],
              "extraction": {}}
    out1 = asyncio.run(node(st_dup))
    q1 = out1["escalation_queue"]
    check("first pass produces a binding_uncertain escalation",
          any(e.get("kind") == "binding_uncertain" for e in q1), str(q1))
    # second verifier→triage pass re-detects the SAME standing defect → must NOT double it
    out2 = asyncio.run(node(dict(st_dup, escalation_queue=q1)))
    check("re-detected standing defect is NOT duplicated in the queue",
          len(out2["escalation_queue"]) == len(q1), f"{len(q1)}→{len(out2['escalation_queue'])}")

    print("-" * 60)
    if fails:
        print(f"P3-M6 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M6 VERIFY: PASS — triage taxonomy + budget/recur-guard + repair catalog + graph_selfcorrecting loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
