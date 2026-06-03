"""
HGVS-OPTIONS Option 1 — "HGVS never escalates to SME" gate. Offline; deterministic.

User's locked decision: invalid_hgvs flags must NEVER reach the SME queue for live
review. Two halves:

  (A) The VERIFIER trusts grounded extractions. If the record carries
      `hgvs_normalized.valid == True`, OR `evidence_block_ids` (the agent cited a
      block), OR `occurrences[].block_id` (same idea), the byte-level HGVS check
      is skipped. The byte check only runs for UNCITED values (the rare
      hallucination path).

  (B) The rare uncited malformed value that does flag goes through VMAW. With
      `invalid_hgvs` added to `_DROPPABLE_ON_UNRESOLVED`, VMAW drops the field
      when it can't ground it — the dropped payload is preserved in the queue
      for audit but never lands as a live SME item.

This gate codifies both. It's the regression net for the user's "never escalate
HGVS" requirement.

Run:  PYTHONPATH=. python scripts/gates/gate_v4_hgvs_no_sme.py
"""

from __future__ import annotations

import sys

from pipeline.triage import build_defects
from pipeline.vmaw import VMAWAgent, _DROPPABLE_ON_UNRESOLVED
from verification.hgvs_validity import find_malformed_hgvs, hgvs_field_valid


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] Verifier trusts grounded values — three live failure modes silenced")
    grounded_env = {"Genomic_Variant_umbrella": {"Genomic_Variants": [
        # v0 — clean ASCII, grounded via evidence_block_ids (agent didn't run hgvs_validate)
        {"gene_studied": "KRAS", "amino_acid_change": "p.G12D",
         "evidence_block_ids": ["b30"]},
        # v1 — clean ASCII, grounded via hgvs_normalized.valid
        {"gene_studied": "TP53", "amino_acid_change": "p.C242F",
         "hgvs_normalized": {"valid": True, "canonical": "p.C242F"}},
        # v2 — OCR noise (unicode hyphen U+2010 instead of '.') BUT grounded
        {"gene_studied": "BRAF", "amino_acid_change": "p‐L190R",
         "evidence_block_ids": ["b47"]},
        # v3 — grounded only by occurrences[].block_id (no evidence_block_ids at root)
        {"gene_studied": "JAK2", "amino_acid_change": "p.V617F",
         "occurrences": [{"block_id": "b50", "surface": "V617F"}]},
    ]}}
    malformed = find_malformed_hgvs(grounded_env)
    check("zero grounded HGVS values flagged by the verifier",
          len(malformed) == 0, str([m["ref"] for m in malformed]))

    print("[2] Each individual trust path works (defensive — protects regressions)")
    check("trust path: hgvs_normalized.valid",
          hgvs_field_valid("amino_acid_change", "anything-here-even-bad!",
                           record={"hgvs_normalized": {"valid": True}}))
    check("trust path: evidence_block_ids",
          hgvs_field_valid("amino_acid_change", "p‐G12D",
                           record={"evidence_block_ids": ["b1"]}))
    check("trust path: occurrences[].block_id",
          hgvs_field_valid("amino_acid_change", "p‐G12D",
                           record={"occurrences": [{"block_id": "b1"}]}))

    print("[3] Safety net still catches UNCITED genuinely-malformed values")
    uncited_env = {"Genomic_Variant_umbrella": {"Genomic_Variants": [
        {"gene_studied": "FAKE", "amino_acid_change": "this is not hgvs at all"},
    ]}}
    uncited_malformed = find_malformed_hgvs(uncited_env)
    check("uncited malformed is still flagged (safety net active)",
          len(uncited_malformed) == 1, str(uncited_malformed))

    print("[4] Uncited VALID HGVS is accepted (no false flag)")
    uncited_valid = {"Genomic_Variant_umbrella": {"Genomic_Variants": [
        {"gene_studied": "JAK2", "amino_acid_change": "p.V617F"},  # no citation, clean
    ]}}
    check("uncited valid HGVS not flagged",
          not find_malformed_hgvs(uncited_valid))

    print("[5] VMAW drops uncited malformed instead of escalating to SME")
    check("invalid_hgvs is in _DROPPABLE_ON_UNRESOLVED",
          "invalid_hgvs" in _DROPPABLE_ON_UNRESOLVED,
          str(_DROPPABLE_ON_UNRESOLVED))
    # Triage builds the defect from the verifier scorecard.
    state = {
        "extraction": uncited_env,
        "verifier_scorecards": [{
            "verifier_name": "hgvs_validity", "passed": True,
            "field_errors": uncited_malformed,
        }],
        "binding_verifier": {"refuted": 0, "uncertain": 0},
        "binding_items": [],
    }
    defects = [d for d in build_defects(state) if d.defect_type == "invalid_hgvs"]
    check("triage builds one invalid_hgvs defect from the safety net",
          len(defects) == 1, str(defects))
    queue_item = {"kind": "invalid_hgvs",
                  "ref": defects[0].target_ref,
                  "section": "Genomic_Variant_umbrella",
                  "detail": defects[0].detail}
    # No LLM hooks → VMAW returns unresolved → drop branch fires (per _DROPPABLE_ON_UNRESOLVED).
    vmaw_out = VMAWAgent().resolve({
        "extraction": uncited_env, "escalation_queue": [queue_item],
        "doc_profile": {"blocks": []},
    })
    new_q = vmaw_out["escalation_queue"]
    check("no invalid_hgvs item remains in the queue (would be live SME work)",
          not any(it.get("kind") == "invalid_hgvs" for it in new_q),
          str([it.get("kind") for it in new_q]))
    check("one dropped_ungroundable audit item remains (preserved for restore)",
          sum(1 for it in new_q if it.get("kind") == "dropped_ungroundable") == 1,
          str(new_q))
    var0 = vmaw_out["extraction"]["Genomic_Variant_umbrella"]["Genomic_Variants"][0]
    check("malformed field is removed from the record (no garbage shipped downstream)",
          "amino_acid_change" not in var0 or var0.get("amino_acid_change") is None,
          str(var0))
    check("other fields of the record are preserved (granular drop)",
          var0.get("gene_studied") == "FAKE", str(var0))

    print("-" * 60)
    if fails:
        print(f"V4-HGVS-NO-SME VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-HGVS-NO-SME VERIFY: PASS — HGVS never reaches SME "
          "(grounded → silent; uncited malformed → dropped via VMAW).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
