"""
Phase 3 M8c gate — field-trace assembler + plain/technical explanations
(ui/phase1/field_trace.py). Pure, offline.

Checks:
  [1] assemble_field_trace merges agent_trace + scorecards + repair_log + vmaw_log
      into ONE timeline, filtered to the field, ordered extraction→verification→
      repair→resolution, each step carrying both a technical and a plain line.
  [2] filtering: steps for a different section/ref are excluded.
  [3] explain(mode='plain') is jargon-free (no kind codes / refs / node names) and
      includes the VMAW suggestion; explain(mode='technical') includes them.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m8c_field_timeline.py
"""

from __future__ import annotations

import sys

from ui.phase1.field_trace import assemble_field_trace, explain, why

_REF = "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]"
_SEC = "other_molecular_biomarker_umbrella"


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    agent_trace = [
        {"step": 0, "agent": "Extractor", "team": "molecular_biomarker_team", "section": _SEC,
         "output_summary": "produced 1 populated field(s)", "verdict": "extracted", "confidence": 0.6},
        {"step": 1, "agent": "CoverageAuditor", "team": "molecular_biomarker_team", "section": _SEC,
         "output_summary": "coverage_ok=False, gap_signal=True", "verdict": "gap_flagged"},
        # a step from ANOTHER section — must be excluded
        {"step": 0, "agent": "Extractor", "team": "metadata_team", "section": "report_metadata",
         "output_summary": "produced 5 populated field(s)", "verdict": "extracted", "confidence": 0.95},
    ]
    scorecards = [
        {"verifier_name": "schema_validator", "passed": True, "field_errors": [], "notes": "ok"},
        {"verifier_name": "link_consistency", "passed": False, "notes": "refuted bind",
         "field_errors": [{"field_name": _REF}]},
    ]
    repair_log = [
        {"cycle": 1, "action": "re_extract_team", "team": "molecular_biomarker_team", "section": _SEC,
         "target_ref": _REF, "outcome": "re_extracted"},
        {"cycle": 1, "action": "drop_and_flag", "section": "significant_findings",
         "target_ref": "significant_findings.specimen_findings[3]", "outcome": "dropped"},  # other ref
    ]
    vmaw_log = [
        {"resolution": {"item_ref": _REF, "capability": "va", "status": "proposed_for_sme",
                        "grounded": False, "contested": True, "resolved_value": "negative"}},
    ]

    print("[1] merged, ordered timeline")
    steps = assemble_field_trace(ref=_REF, section=_SEC, agent_trace=agent_trace,
                                 scorecards=scorecards, repair_log=repair_log, vmaw_log=vmaw_log)
    phases = [s["phase"] for s in steps]
    check("phases ordered extraction→verification→repair→resolution",
          phases == ["extraction", "extraction", "verification", "repair", "resolution"], str(phases))
    check("every step has technical + plain", all(s.get("technical") and s.get("plain") for s in steps))
    check("VMAW resolution step present", any(s["node"].startswith("VMAW") for s in steps))

    print("[2] filtering excludes other section / ref")
    check("metadata extraction step excluded",
          not any("5 populated" in s["technical"] for s in steps))
    check("other-ref repair (drop_and_flag) excluded",
          not any("specimen_findings[3]" in s["technical"] for s in steps))

    print("[3] plain vs technical explanation")
    item = {"kind": "binding_refuted", "ref": _REF, "section": _SEC, "detail": "EGFR/HER2 mis-bind",
            "vmaw_proposal": {"value": "negative", "citation_block_ids": ["b2"],
                              "grounded": False, "contested": True}}
    plain = explain(item, mode="plain")
    tech = explain(item, mode="technical")
    jargon = ["binding_refuted", "sme_flag", _REF, "vmaw_proposal", "kind="]
    check("plain text is jargon-free", not any(j in plain for j in jargon), plain[:80])
    check("plain text includes the suggestion value", "negative" in plain)
    check("plain text invites approve/edit/keep", "approve" in plain.lower())
    check("technical text includes kind + ref", "binding_refuted" in tech and _REF in tech)

    print("[3c] binding-verifier verdicts threaded into the trace (toggle-gated)")
    binding_items = [
        {"ref": "other_molecular_biomarkers[0].findings[0]", "check": "V2", "verdict": "refuted",
         "evidence": "the span attributes 'amplified' to EGFR, not HER2"},
        {"ref": "other_molecular_biomarkers[9].findings[0]", "check": "V1", "verdict": "uncertain",
         "evidence": "unrelated record"},
    ]
    steps_b = assemble_field_trace(ref=_REF, section=_SEC, agent_trace=agent_trace,
                                   scorecards=scorecards, repair_log=repair_log, vmaw_log=vmaw_log,
                                   binding_items=binding_items)
    bind_steps = [s for s in steps_b if s.get("kind") == "binding"]
    check("matching binding verdict added as a step", len(bind_steps) == 1, str(len(bind_steps)))
    check("non-matching binding ref excluded",
          not any("[9]" in s.get("technical", "") for s in bind_steps))
    check("binding step carries the evidence as its reasoning",
          bind_steps and "EGFR" in bind_steps[0]["reasoning"])
    check("binding step plain line is jargon-free",
          bind_steps and not any(j in bind_steps[0]["plain"] for j in ["V2", _REF, "refuted"]),
          bind_steps[0]["plain"][:60] if bind_steps else "")

    print("[3d] per-field reasoning (auditor/arbiter singled out the field; extraction rationale)")
    at = [{"step": 0, "agent": "CoverageAuditor", "section": _SEC,
           "output_summary": "gap", "verdict": "gap_flagged", "reasoning": "section looked off",
           "field_reasons": {"result": "missed — HER2 result is in the ancillary table"}},
          {"step": 1, "agent": "Arbiter", "section": _SEC,
           "output_summary": "RE_EXTRACT", "verdict": "RE_EXTRACT", "reasoning": "re-extract section",
           "field_reasons": {"result": "look in the ancillary studies row"}},
          {"step": 2, "agent": "Extractor", "section": _SEC,
           "output_summary": "produced 1 field", "verdict": "extracted", "reasoning": "section thought"}]
    ref_result = _SEC + ".other_molecular_biomarkers[0].findings[0].result"
    steps_f = assemble_field_trace(ref=ref_result, section=_SEC, agent_trace=at,
                                   field_rationale="taken verbatim from block b2")
    audit = next(s for s in steps_f if s["node"] == "CoverageAuditor")
    arb = next(s for s in steps_f if s["node"] == "Arbiter")
    extr = next(s for s in steps_f if s["node"] == "Extractor")
    check("auditor shows the FIELD reason (not the section note)", "ancillary table" in audit["reasoning"], audit["reasoning"])
    check("auditor field reason is scoped 'field'", audit.get("reasoning_scope") == "field", audit.get("reasoning_scope"))
    check("arbiter shows the FIELD hint", "ancillary studies row" in arb["reasoning"], arb["reasoning"])
    check("extractor shows the field rationale", extr["reasoning"] == "taken verbatim from block b2")
    check("extractor rationale is scoped 'field'", extr.get("reasoning_scope") == "field", extr.get("reasoning_scope"))
    # a field the agents did NOT single out → section-level note, LABELLED as such
    steps_o = assemble_field_trace(ref=_SEC + ".other_molecular_biomarkers[0].biomarker_name",
                                   section=_SEC, agent_trace=at)
    audit_o = next(s for s in steps_o if s["node"] == "CoverageAuditor")
    check("non-singled field → auditor section note", audit_o["reasoning"] == "section looked off", audit_o["reasoning"])
    check("non-singled note is scoped 'section' (so the UI won't claim it's this field)",
          audit_o.get("reasoning_scope") == "section", audit_o.get("reasoning_scope"))

    print("[3e] persisted scorecard shape (error_locs) is threaded into verification")
    # the verification_v2 artifact flattens field_errors → error_locs (list of strings)
    persisted_cards = [{"verifier_name": "link_consistency", "passed": False,
                        "notes": "binding refuted near this record",
                        "error_locs": [_REF + ".findings[0]"]}]
    steps_p = assemble_field_trace(ref=_REF + ".findings[0].result", section=_SEC,
                                   agent_trace=[], scorecards=persisted_cards)
    vstep = next((s for s in steps_p if s["phase"] == "verification"), None)
    check("error_locs scorecard produces a verification step", vstep is not None)
    check("scorecard naming the ref is scoped 'field'",
          vstep and vstep.get("reasoning_scope") == "field", vstep.get("reasoning_scope") if vstep else None)

    print("[5] linking phase — linker relationships surface as their own steps")
    links = [{"from_ref": _SEC + ".other_molecular_biomarkers[0]",
              "to_ref": "tested_biomarker_umbrella.tested_biomarkers[0]",
              "type": "variant_on_panel", "rationale": "Same HGNC-normalized gene 'JAK2'.",
              "method": "deterministic", "confidence": 1.0}]
    steps_l = assemble_field_trace(
        ref=_SEC + ".other_molecular_biomarkers[0].findings[0].result",
        section=_SEC, agent_trace=[], links=links)
    lstep = next((s for s in steps_l if s["phase"] == "linking"), None)
    check("record-level link surfaces on a sub-field", lstep is not None)
    check("linking step carries the linker rationale, scoped 'field'",
          lstep and "JAK2" in lstep["reasoning"] and lstep.get("reasoning_scope") == "field")
    check("linking step plain line names the relationship",
          lstep and "panel" in lstep["plain"].lower(), lstep["plain"][:60] if lstep else "")
    steps_n = assemble_field_trace(ref="report_metadata.Patient_First_Name",
                                   section="report_metadata", agent_trace=[], links=links)
    check("unrelated field has NO linking step",
          not any(s["phase"] == "linking" for s in steps_n))
    steps_ord = assemble_field_trace(ref=_REF, section=_SEC, agent_trace=agent_trace,
                                     scorecards=scorecards, links=links)
    order = [s["phase"] for s in steps_ord]
    check("linking ordered after extraction, before verification",
          ("linking" in order and "verification" in order
           and order.index("linking") > max(i for i, p in enumerate(order) if p == "extraction")
           and order.index("linking") < order.index("verification")), str(order))

    print("[4] why-flagged (link reason)")
    why_plain = why(item, mode="plain")
    why_tech = why(item, mode="technical")
    check("link why-flagged explains binding ambiguity (jargon-free)",
          ("row" in why_plain.lower() or "which test" in why_plain.lower())
          and not any(j in why_plain for j in jargon), why_plain[:80])
    check("why technical includes defect kind", "binding_refuted" in why_tech)
    check("recall miss has its own why",
          "section" in why({"kind": "recall_floor_loud", "ref": "x"}, mode="plain").lower())
    # M10c: attribution-contest explanation + why
    why_attr = why({"kind": "attribution_contested", "ref": "x"}, mode="plain")
    check("attribution contest why mentions multiple records (jargon-free)",
          "record" in why_attr.lower() and not any(j in why_attr for j in jargon))
    expl_attr = explain({"kind": "attribution_contested", "ref": "x", "section": "significant_findings"}, mode="plain")
    check("attribution_contested plain explanation asks to confirm the record (jargon-free)",
          "record" in expl_attr.lower() and not any(j in expl_attr for j in ["attribution_contested", "kind=", "ref="]))

    print("-" * 60)
    if fails:
        print(f"P3-M8c VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M8c VERIFY: PASS — field timeline merged/ordered/filtered; plain + technical explanations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
