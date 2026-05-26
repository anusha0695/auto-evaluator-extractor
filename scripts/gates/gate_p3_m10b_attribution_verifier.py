"""
Phase 3 M10b gate — owner-keyed attribution verifier (verification/attribution.py).
Pure, offline.

Checks:
  [1] attribution_map loads; schema-completeness clean against the real v3 schema
      (every owner_key + attribute is a real field of the object the path lands on);
      a bad map is caught.
  [2] no hook → advisory 'unverified' (passed=True, never silently grounded), with a
      per-target metrics tally.
  [3] owner anchoring: present owner_key → anchored_by 'owner_key'; null owner_key →
      anchored_by 'position'; the null owner_key is NOT itself emitted as a defect.
  [4] multiplicity n_owners = sibling count (does NOT gate the check).
  [5] injected hook autonomy gate: grounded → no error; contested → loud error;
      ungrounded → quiet error. Covers specimen + biomarker-finding + no-result targets.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m10b_attribution_verifier.py
"""

from __future__ import annotations

import sys
import tempfile

from core.schema_loader import SchemaLoader
from verification.attribution import AttributionVerifier, validate_against_schema

MAP = "config/attribution_map.yaml"


def _envelope():
    return {
        "significant_findings": {"specimen_findings": [{
            "specimen": [
                {"specimen_id": "A", "tissue_type": "Breast", "laterality": "Right",
                 "occurrences": [{"block_id": "b1"}]},
                {"specimen_id": None, "tissue_type": "Lymph node", "laterality": "Left",
                 "occurrences": [{"block_id": "b2"}]},
            ],
            "histologic_findings": [{"finding": "IDC", "interpretation": "grade 2"}],
        }]},
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
            {"biomarker_name": "JAK2", "findings": [
                {"result": "Detected", "method": "PCR", "interpretation": "POSITIVE",
                 "occurrences": [{"block_id": "b3"}]}]}]},
        "tested_biomarker_umbrella": {
            "biomarkers_tested_no_result": [{"name": "KRAS", "reason": "QNS", "details": "insufficient"}]},
    }


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")

    print("[1] map loads + schema-completeness")
    issues = validate_against_schema(MAP, sl)
    check("real attribution_map references only real schema sections/fields", issues == [], str(issues))
    bad = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    bad.write("attribution_targets:\n  - {name: x, section: significant_findings, "
              "array_keys: [specimen_findings, specimen], owner_key: NOPE, attributes: [alsobad]}\n")
    bad.flush()
    check("bad owner_key/attribute caught", len(validate_against_schema(bad.name, sl)) >= 2)

    print("[2] no hook → advisory 'unverified'")
    sc = AttributionVerifier(mapping_path=MAP).verify(envelope=_envelope())
    check("verifier_name == attribution", sc["verifier_name"] == "attribution")
    check("advisory passed=True", sc["passed"] is True)
    check("all candidates unverified (no hook)",
          all(e["status"] == "unverified" for e in sc["field_errors"]) and sc["field_errors"])
    check("metrics tally present per target", "specimen_attributes" in sc["metrics"]
          and sc["metrics"]["specimen_attributes"]["checked"] >= 1)

    print("[3] owner anchoring + null-owner not a defect")
    errs = sc["field_errors"]
    sp_errs = [e for e in errs if e["target"] == "specimen_attributes"]
    a_anchor = {e["owner_ref"]: e["anchored_by"] for e in sp_errs}
    ref0 = "significant_findings.specimen_findings[0].specimen[0]"
    ref1 = "significant_findings.specimen_findings[0].specimen[1]"
    check("specimen A (id present) anchored by owner_key", a_anchor.get(ref0) == "owner_key", str(a_anchor))
    check("specimen with null id anchored by position", a_anchor.get(ref1) == "position")
    check("null owner_key is NOT itself emitted as a defect",
          not any(e["attribute"] == "specimen_id" for e in errs))
    check("position anchor surfaces the [idx] as owner_id",
          any(e["owner_ref"] == ref1 and e["owner_id"] == "specimen[1]" for e in sp_errs), str(sp_errs))

    print("[4] multiplicity recorded (does not gate)")
    check("specimen n_owners == 2", all(e["n_owners"] == 2 for e in sp_errs))
    bm_errs = [e for e in errs if e["target"] == "biomarker_finding_attributes"]
    check("single biomarker finding still checked (n_owners==1)",
          bm_errs and all(e["n_owners"] == 1 for e in bm_errs))

    print("[5] injected hook autonomy gate")
    def hook(*, owner_key, owner_id, anchored_by, attribute, value, candidate_block_ids, n_owners):
        if attribute == "tissue_type":
            return "contested"        # bleed risk between specimen A/B
        if attribute == "method":
            return "ungrounded"
        return "grounded"             # everything else cleanly attributed
    sc2 = AttributionVerifier(mapping_path=MAP, attribution_fn=hook).verify(envelope=_envelope())
    e2 = sc2["field_errors"]
    tt = [e for e in e2 if e["attribute"] == "tissue_type"]
    check("tissue_type → contested + loud", tt and all(e["status"] == "contested" and e["strictness"] == "loud" for e in tt))
    check("grounded attributes produce NO error (e.g. laterality)",
          not any(e["attribute"] == "laterality" for e in e2))
    check("ungrounded method → quiet error",
          any(e["attribute"] == "method" and e["status"] == "ungrounded" and e["strictness"] == "quiet" for e in e2))
    check("metrics counts grounded", sc2["metrics"]["specimen_attributes"]["grounded"] >= 1)
    check("no-result target checked (KRAS reason/details)",
          "tested_no_result_attributes" in sc2["metrics"]
          and sc2["metrics"]["tested_no_result_attributes"]["checked"] >= 1)

    print("[6] real-signature hook receives candidate block TEXTS (vs id-only stub)")
    seen_texts: list = []
    def hook_blocks(*, candidate_blocks, **kw):       # real-signature (Gemini-backed shape)
        seen_texts.extend([b.get("text") for b in (candidate_blocks or [])])
        return "grounded" if any(b.get("text") for b in (candidate_blocks or [])) else "ungrounded"
    blocks = [{"block_id": "b1", "text": "Part A: right breast"},
              {"block_id": "b2", "text": "Part B: left axillary node"}]
    sc3 = AttributionVerifier(mapping_path=MAP, attribution_fn=hook_blocks).verify(
        envelope=_envelope(), blocks=blocks)
    check("hook accepting candidate_blocks got block text", any(seen_texts), str(seen_texts[:3]))
    check("grounded hook → no field_errors for specimen", sc3["metrics"]["specimen_attributes"]["grounded"] >= 1)

    print("-" * 60)
    if fails:
        print(f"P3-M10b VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M10b VERIFY: PASS — owner-keyed attribution (anchor/fallback/autonomy/multiplicity).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
