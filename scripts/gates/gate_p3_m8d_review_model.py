"""
Phase 3 M8d gate — SME decision model + apply logic (ui/phase1/sme_decisions.py).
Pure, offline (a fake `load` for artifacts; no Streamlit).

Checks:
  [1] build_review_model sorts proposals-first, enriches each item with a trace +
      plain/technical explanation, and computes counts.
  [2] apply_sme_decision: approve sets the VMAW value + clears needs_review +
      stamps sme_review; edit sets the SME value; keep_flagged records only.
  [3] the ORIGINAL extraction is never mutated; verbatim occurrences are preserved.
  [4] decision record shape (ref/action/applied_value/before/ts).

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m8d_review_model.py
"""

from __future__ import annotations

import sys

from ui.phase1.sme_decisions import apply_sme_decision, build_review_model

_REF = "significant_findings.specimen_findings[0].pTNM_staging_details"


def _fake_artifacts():
    return {
        "escalation_queue": [
            {"kind": "unresolved", "ref": "x.y[0]", "section": "x", "vmaw_note": {"status": "unresolved"}},
            {"kind": "needs_review", "ref": _REF, "section": "significant_findings",
             "vmaw_proposal": {"value": "pT2", "citation_block_ids": ["b1"], "grounded": True, "contested": False}},
        ],
        "agent_trace": [{"step": 0, "agent": "Extractor", "section": "significant_findings",
                         "output_summary": "produced 3 populated field(s)", "verdict": "extracted", "confidence": 0.7}],
        "repair_log": [],
        "vmaw_log": [{"resolution": {"item_ref": "z", "status": "auto_applied"}}],
    }


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    arts = _fake_artifacts()
    load = lambda doc, kind: arts.get(kind)  # noqa: E731

    print("[1] build_review_model")
    model = build_review_model("demo", load=load)
    check("proposal item sorted first", model["items"][0].get("vmaw_proposal") is not None)
    check("each item enriched with trace + explanations",
          all("_trace" in it and "_plain" in it and "_technical" in it for it in model["items"]))
    check("counts: queue=2, proposals=1, auto_applied=1",
          model["counts"]["queue"] == 2 and model["counts"]["proposals"] == 1
          and model["counts"]["auto_applied"] == 1, str(model["counts"]))

    print("[2] apply: approve / edit / keep_flagged")
    extraction = {"significant_findings": {"specimen_findings": [
        {"specimen_id": "A",
         "pTNM_staging_details": {"pTNM_stage": "pT2", "needs_review": True, "review_reason": "OCR"},
         "occurrences": [{"block_id": "b1", "surface": "pT2 pN1"}]}]}}

    reviewed_a, dec_a = apply_sme_decision(extraction, ref=_REF, action="approve", proposal_value="pT2")
    rec_a = reviewed_a["significant_findings"]["specimen_findings"][0]["pTNM_staging_details"]
    check("approve clears needs_review", rec_a.get("needs_review") is False)
    check("approve stamps sme_review with value", rec_a.get("sme_review", {}).get("value") == "pT2")

    reviewed_e, dec_e = apply_sme_decision(extraction, ref=_REF, action="edit", value="pT3")
    rec_e = reviewed_e["significant_findings"]["specimen_findings"][0]["pTNM_staging_details"]
    check("edit records the SME value", rec_e.get("sme_review", {}).get("value") == "pT3")
    check("edit decision applied_value=pT3", dec_e["applied_value"] == "pT3")

    reviewed_k, dec_k = apply_sme_decision(extraction, ref=_REF, action="keep_flagged")
    rec_k = reviewed_k["significant_findings"]["specimen_findings"][0]["pTNM_staging_details"]
    check("keep_flagged leaves needs_review set", rec_k.get("needs_review") is True)
    check("keep_flagged records the action", rec_k.get("sme_review", {}).get("action") == "keep_flagged")

    print("[3] original immutable + occurrences preserved")
    orig_rec = extraction["significant_findings"]["specimen_findings"][0]["pTNM_staging_details"]
    check("original still needs_review=True", orig_rec.get("needs_review") is True)
    check("original has no sme_review", "sme_review" not in orig_rec)
    occ = reviewed_a["significant_findings"]["specimen_findings"][0]["occurrences"]
    check("verbatim occurrences preserved in reviewed copy",
          occ == [{"block_id": "b1", "surface": "pT2 pN1"}], str(occ))

    print("[4] decision record shape")
    check("record has ref/action/applied_value/before/ts",
          {"ref", "action", "applied_value", "before", "ts"} <= set(dec_a),
          str(sorted(dec_a)))
    check("approve applied VMAW proposal value", dec_a["applied_value"] == "pT2")

    print("-" * 60)
    if fails:
        print(f"P3-M8d VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M8d VERIFY: PASS — review model + decision apply (immutable original, occurrences safe).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
