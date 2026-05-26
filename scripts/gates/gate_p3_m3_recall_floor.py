"""
Phase 3 M3 gate — block-role recall floor (verification/recall_floor.py).

Checks: (1) the mapping passes the schema-completeness check (no rule references a
non-existent section/field) and a deliberately-bad rule is caught; (2) the detector
flags a role-block-with-empty-field and does NOT flag when the field is populated;
(3) the AI re-read confirms true-absence (drop) vs present (confirmed_miss) and is
MEMOIZED in block_reads (not re-read twice); (4) a role not on the page raises no
expectation. No LLM — the re-read is a mock.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m3_recall_floor.py
"""

from __future__ import annotations

import sys
import tempfile

from core.schema_loader import SchemaLoader
from verification.recall_floor import RecallFloorVerifier, validate_against_schema

MAP = "config/recall_floor.yaml"


def _specimen(ptnm=None):
    return {"significant_findings": {"specimen_findings": [
        {"specimen": [{"specimen_id": "A"}],
         "pTNM_staging_details": {"pTNM_stage": ptnm},
         "gross_description": {"text": "x"}}]}}


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")

    print("[1] schema-completeness of the mapping")
    issues = validate_against_schema(MAP, sl)
    check("real mapping references only real schema sections/fields", issues == [], str(issues))
    # a deliberately bad rule is caught
    bad = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    bad.write("role_field_rules:\n  - {role: x, section: NOPE_umbrella, check: scalar, path: [zzz], strictness: loud}\n")
    bad.flush()
    bad_issues = validate_against_schema(bad.name, sl)
    check("bad rule (fake section) is caught", len(bad_issues) >= 1, str(bad_issues))

    print("[2] detector: empty role-block field flagged; populated not flagged")
    bps = [{"block_id": "b9", "text_role": "synoptic_report"}]
    v = RecallFloorVerifier(mapping_path=MAP)  # no re-read → unconfirmed
    sc_missing = v.verify(envelope=_specimen(ptnm=None), block_profiles=bps)
    ptnm_miss = [m for m in sc_missing["field_errors"] if "pTNM_stage" in m["field_name"]]
    check("synoptic_report + empty pTNM → candidate miss (loud, unconfirmed)",
          len(ptnm_miss) == 1 and ptnm_miss[0]["strictness"] == "loud"
          and ptnm_miss[0]["status"] == "unconfirmed", str(ptnm_miss))
    check("scorecard advisory (passed=True)", sc_missing["passed"] is True)
    sc_present = v.verify(envelope=_specimen(ptnm="pT2 pN1a M0"), block_profiles=bps)
    check("populated pTNM → no pTNM miss",
          not any("pTNM_stage" in m["field_name"] for m in sc_present["field_errors"]))

    print("[3] true-absence re-read + memoization")
    calls = {"n": 0}
    def reread_absent(**kw):
        calls["n"] += 1
        return False   # field genuinely NOT in the block → true negative
    v_abs = RecallFloorVerifier(mapping_path=MAP, reread_fn=reread_absent)
    cache: dict = {}
    sc = v_abs.verify(envelope=_specimen(ptnm=None), block_profiles=bps, block_reads=cache)
    check("re-read 'absent' drops the pTNM miss",
          not any("pTNM_stage" in m["field_name"] for m in sc["field_errors"]))
    check("determination cached in block_reads", any("pTNM_stage" in k for k in cache))
    n_after_first = calls["n"]
    v_abs.verify(envelope=_specimen(ptnm=None), block_profiles=bps, block_reads=cache)
    check("memoized — re-read not called again for same block/field", calls["n"] == n_after_first,
          f"calls={calls['n']}")

    def reread_present(**kw):
        return True   # field IS there, extractor missed it → real miss
    v_pres = RecallFloorVerifier(mapping_path=MAP, reread_fn=reread_present)
    sc_p = v_pres.verify(envelope=_specimen(ptnm=None), block_profiles=bps, block_reads={})
    check("re-read 'present' → confirmed_miss",
          any(m["status"] == "confirmed_miss" and "pTNM_stage" in m["field_name"]
              for m in sc_p["field_errors"]))

    print("[4] role not on the page → no expectation")
    sc_norole = v.verify(envelope=_specimen(ptnm=None), block_profiles=[{"block_id": "b1", "text_role": "vendor_branding"}])
    check("no synoptic block → no pTNM miss",
          not any("pTNM_stage" in m["field_name"] for m in sc_norole["field_errors"]))

    print("-" * 60)
    if fails:
        print(f"P3-M3 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M3 VERIFY: PASS — recall floor detects misses, schema-checked, re-read memoized.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
