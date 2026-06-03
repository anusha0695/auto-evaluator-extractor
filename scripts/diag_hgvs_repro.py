"""
Reproduce the live failure from escalation_queue_banded.json with dummy data,
end-to-end, no LLM, no Mac needed.

Constructs three variant records that mirror the screenshot:
  v0 — amino_acid_change p.G12D    (clean ASCII; agent did NOT emit hgvs_normalized)
  v1 — amino_acid_change p.C242F   (clean ASCII; agent DID emit hgvs_normalized.valid=true)
  v2 — amino_acid_change p‐L190R (OCR noise — unicode hyphen instead of '.')

Each record cites evidence_block_ids (the agent grounded all three). We then run:
  - verification.hgvs_validity.find_malformed_hgvs (current → buggy)
  - pipeline.triage.build_defects   (turns those into invalid_hgvs defects)
  - and we print exactly what would land in the SME queue.

After applying HGVS-OPTIONS Option 1 (trust grounded), this script will pass; before
the patch it fails on v0 and v2 (the two cases in the screenshot).
"""
from __future__ import annotations

import json
import sys

from verification.hgvs_validity import find_malformed_hgvs
from pipeline.triage import build_defects


def make_envelope() -> dict:
    return {
        "Genomic_Variant_umbrella": {
            "Genomic_Variants": [
                # v0 — clean ASCII, agent grounded but did NOT call hgvs_validate tool
                {
                    "gene_studied": "KRAS",
                    "amino_acid_change": "p.G12D",
                    "evidence_block_ids": ["b30"],
                    "occurrences": [{"block_id": "b30", "surface": "p.G12D"}],
                },
                # v1 — clean ASCII, agent grounded AND emitted hgvs_normalized.valid
                {
                    "gene_studied": "TP53",
                    "amino_acid_change": "p.C242F",
                    "hgvs_normalized": {"valid": True, "canonical": "p.C242F"},
                    "evidence_block_ids": ["b35"],
                    "occurrences": [{"block_id": "b35", "surface": "C242F"}],
                },
                # v2 — OCR noise (unicode hyphen U+2010 instead of '.'), but agent grounded it
                {
                    "gene_studied": "BRAF",
                    "amino_acid_change": "p‐L190R",
                    "evidence_block_ids": ["b47"],
                    "occurrences": [{"block_id": "b47", "surface": "p‐L190R"}],
                },
            ]
        }
    }


def section_layout_to_state(envelope: dict) -> dict:
    """Wrap the envelope into the state shape the verifiers and triage consume."""
    # Mimic what run_verifier_suite would have produced for hgvs_validity.
    malformed = find_malformed_hgvs(envelope)
    return {
        "extraction": envelope,
        "verifier_scorecards": [{
            "verifier_name": "hgvs_validity",
            "passed": True,                       # advisory; the defects fire from field_errors
            "field_errors": malformed,
            "notes": (f"{len(malformed)} malformed HGVS leaf(s) → needs_review"
                      if malformed else "no malformed HGVS"),
        }],
        "binding_verifier": {"refuted": 0, "uncertain": 0},
        "binding_items": [],
    }


def main() -> int:
    env = make_envelope()
    state = section_layout_to_state(env)

    print("=" * 64)
    print("VERIFIER (verification.hgvs_validity.find_malformed_hgvs)")
    print("=" * 64)
    malformed = state["verifier_scorecards"][0]["field_errors"]
    print(f"flagged {len(malformed)} field(s):")
    for m in malformed:
        v = m["value"]
        hex_dump = " ".join(f"{ord(c):02x}" for c in v)
        print(f"  - ref={m['ref']}  value={v!r}  bytes=[{hex_dump}]")

    print()
    print("=" * 64)
    print("TRIAGE (pipeline.triage.build_defects → escalation queue)")
    print("=" * 64)
    defects = [d for d in build_defects(state) if d.defect_type == "invalid_hgvs"]
    print(f"would queue {len(defects)} invalid_hgvs defect(s) to SME:")
    for d in defects:
        print(f"  - ref={d.target_ref}  detail={d.detail}")

    # Expected behavior post-Option 1:
    #   v0 → trusted via evidence_block_ids → NOT flagged
    #   v1 → trusted via hgvs_normalized.valid → NOT flagged
    #   v2 → OCR noise BUT grounded via evidence_block_ids → NOT flagged
    expected_post_patch = 0
    print()
    print(f"expected after HGVS-OPTIONS Option 1: {expected_post_patch} flagged")
    print(f"actual now:                          {len(malformed)} flagged")
    print()
    grounded_ok = len(malformed) == expected_post_patch

    # ----- Edge case: the rare UNCITED malformed value ---------------------
    # User said "I never want HGVS escalated to SME at all." With invalid_hgvs added
    # to _DROPPABLE_ON_UNRESOLVED, VMAW now drops any value it can't ground instead
    # of leaving it in the SME queue. Prove that here with an offline VMAW (no hooks
    # → status=unresolved → drop branch fires).
    print("=" * 64)
    print("EDGE CASE: ungrounded malformed value (no agent citation)")
    print("=" * 64)
    from pipeline.vmaw import VMAWAgent
    edge_env = {"Genomic_Variant_umbrella": {"Genomic_Variants": [
        # malformed AND not cited — the rare hallucination path
        {"gene_studied": "FAKE", "amino_acid_change": "this is not hgvs at all"},
    ]}}
    edge_state = section_layout_to_state(edge_env)
    edge_defects = [d for d in build_defects(edge_state) if d.defect_type == "invalid_hgvs"]
    print(f"verifier flagged {len(edge_defects)} (expected: 1, the safety net works)")
    if not edge_defects:
        print("RESULT: FAIL — safety net should still catch ungrounded malformed values")
        return 1

    # Simulate the queue entry triage would have produced.
    queue_item = {"kind": "invalid_hgvs",
                  "ref": edge_defects[0].target_ref,
                  "section": "Genomic_Variant_umbrella",
                  "detail": edge_defects[0].detail}
    edge_out = VMAWAgent().resolve({                   # no LLM hooks → unresolved
        "extraction": edge_env, "escalation_queue": [queue_item],
        "doc_profile": {"blocks": []},
    })
    new_queue = edge_out["escalation_queue"]
    # Expect: 0 items with kind=invalid_hgvs (dropped), 1 audit item kind=dropped_ungroundable.
    # The ref targeted a FIELD (amino_acid_change), so the malformed field is removed
    # from the record — the rest of the record (gene_studied, etc.) is preserved.
    invalid_left = [it for it in new_queue if it.get("kind") == "invalid_hgvs"]
    dropped = [it for it in new_queue if it.get("kind") == "dropped_ungroundable"]
    print(f"after VMAW: invalid_hgvs left in queue = {len(invalid_left)} (expected 0)")
    print(f"            dropped_ungroundable items = {len(dropped)} (expected 1, audit-only)")
    var_after = edge_out["extraction"]["Genomic_Variant_umbrella"]["Genomic_Variants"][0]
    field_gone = "amino_acid_change" not in var_after or var_after.get("amino_acid_change") is None
    print(f"            malformed field removed from record = {field_gone} (expected True)")
    print(f"            other fields of record preserved = {var_after.get('gene_studied')!r} "
          "(expected 'FAKE')")

    edge_ok = (not invalid_left) and len(dropped) == 1 and field_gone

    print()
    if grounded_ok and edge_ok:
        print("FINAL: PASS — grounded HGVS never flagged; ungrounded malformed dropped, not escalated.")
        return 0
    print(f"FINAL: FAIL — grounded_ok={grounded_ok}  edge_ok={edge_ok}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
