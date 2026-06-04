"""
Intra-section dedup gate — locks the GENERIC, field-agnostic reconciliation chain.

The chain under test:

  Extractor (per-mention)
      ↓ emits 2 KRAS records on different pages with different VAFs
  Linker._apply_dedup_policy
      ↓ groups by identity_keys; emits variant_superseded_by link
        (or drops duplicates if all non-identity fields agree)
  to_production._apply_supersession_filter
      ↓ drops the from_ref record from production output

Locks (offline, deterministic; no LLM calls):

  [1] CONFLICT path: two records, same identity (gene + amino_acid_change),
      DIFFERENT non-identity field (VAF). Linker emits ONE
      variant_superseded_by link from the earlier-page record → the later-page
      record. Both records remain in the envelope (audit trail). Production
      filter drops the earlier; only the later (10%) ships.

  [2] IDENTICAL path: two records with the same identity AND every other
      field identical. Linker DROPS the duplicate (one record kept), no
      supersession link emitted. Count_of_Genomic_Variants updated.

  [3] DIFFERENT-VARIANTS path: two records with same gene but DIFFERENT
      amino_acid_change. Linker leaves both alone (not the same entity).
      No supersession link, no drop.

  [4] FIELD-AGNOSTIC: the same machinery fires when the differing field is
      `result` (not VAF) or `method`. No code in linker names specific fields.

  [5] BIOMARKER section: same rule applies to other_molecular_biomarker_umbrella
      (proves the rule is section-generic, not variant-specific).

  [6] WINNER POLICY: higher_page_number wins (universal "later mention is
      amended" rule). Tie on page → first index wins.

  [7] UNRESOLVED IDENTITY: a record missing one of the identity_keys falls
      out as a singleton and never collapses with anything.

Run:  PYTHONPATH=. python scripts/gates/gate_intra_section_dedup.py
"""

from __future__ import annotations

import json
import sys
import tempfile

import yaml

from agents.linker import Linker
from transform.to_production import to_production


fails: list[str] = []
def check(label: str, cond: bool, extra: str = "") -> None:
    tag = "OK  " if cond else "FAIL"
    print(f"  [{tag}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        fails.append(label)


def _link_kras(vaf_a: str, page_a: int, vaf_b: str, page_b: int) -> dict:
    """Two KRAS variants — same identity, different VAFs on different pages."""
    return {
        "Genomic_Variant_umbrella": {
            "count_of_Genomic_Variants": 2,
            "Genomic_Variants": [
                {"gene_studied": "KRAS", "amino_acid_change": "p.G12D",
                 "variant_allele_frequency": vaf_a, "page_number": page_a},
                {"gene_studied": "KRAS", "amino_acid_change": "p.G12D",
                 "variant_allele_frequency": vaf_b, "page_number": page_b},
            ],
        },
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"tested_biomarkers": []},
    }


def _run_linker(envelope: dict) -> dict:
    """Run the linker over an envelope-shaped input. We synthesize the per-section
    `sections` map from the envelope, run `linker.link(sections=...)`, then merge
    the linker's mutated envelope + emitted links back into the input dict so the
    rest of the gate can inspect a single object."""
    linker = Linker()
    sections = {k: v for k, v in envelope.items()
                if k != "links" and isinstance(v, dict)}
    res = linker.link(sections=sections, blocks=[], block_profiles=[])
    # Copy back the linker's mutated section payloads (intra-section dedup may
    # have collapsed arrays and updated count_of_*).
    if isinstance(res.envelope, dict):
        for k, v in res.envelope.items():
            if k in envelope:
                envelope[k] = v
    envelope["links"] = [
        {"from_ref": L.from_ref, "to_ref": L.to_ref, "type": L.type,
         "method": L.method, "rationale": L.rationale}
        for L in (res.links or [])
    ]
    return {"envelope": envelope, "result": res}


def main() -> int:
    print("=" * 64)
    print("Intra-section dedup gate — generic, field-agnostic")
    print("=" * 64)

    # ──── [1] CONFLICT path — KRAS 9.8% (page 1) vs 10% (page 6) ─────
    print("\n[1] CONFLICT path — same identity, different VAF on different pages")
    env = _link_kras("9.8%", 1, "10%", 6)
    out = _run_linker(env)
    links = env["links"]
    sup_links = [L for L in links if L.get("type") == "variant_superseded_by"]
    check("Linker emitted exactly ONE variant_superseded_by link",
          len(sup_links) == 1, str([L.get("type") for L in links]))
    if sup_links:
        L = sup_links[0]
        check("Supersession from earlier page (index 0) → later page (index 1)",
              L.get("from_ref") == "Genomic_Variant_umbrella.Genomic_Variants[0]"
              and L.get("to_ref") == "Genomic_Variant_umbrella.Genomic_Variants[1]",
              f"{L.get('from_ref')} → {L.get('to_ref')}")
        check("Link is method=deterministic (auto-confirms at V4 verifier)",
              L.get("method") == "deterministic")
    variants = env["Genomic_Variant_umbrella"]["Genomic_Variants"]
    check("Both records remain in the envelope (audit trail)",
          len(variants) == 2, f"got {len(variants)}")

    # Production transform applies the supersession filter end-to-end.
    mapping = {"mode": "identity_v4"}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(mapping, fh); mpath = fh.name
    prod = to_production(env, mapping_path=mpath)["genomic_pathology_extraction"]
    pgv = prod["Genomic_Variant_umbrella"]["Genomic_Variants"]
    check("Production emits exactly 1 variant (filter drops the superseded)",
          len(pgv) == 1, f"got {len(pgv)}")
    check("Surviving record carries the LATER-page VAF (10%, not 9.8%)",
          pgv[0].get("variant_allele_frequency") == "10%",
          str(pgv[0].get("variant_allele_frequency")))

    # ──── [2] IDENTICAL path — two records, all fields agree ─────────
    print("\n[2] IDENTICAL path — same identity, ALL fields agree → collapse")
    env2 = _link_kras("9.8%", 1, "9.8%", 1)
    _run_linker(env2)
    v2 = env2["Genomic_Variant_umbrella"]["Genomic_Variants"]
    sup2 = [L for L in env2["links"] if L.get("type") == "variant_superseded_by"]
    check("No supersession link when fields agree", len(sup2) == 0, str(sup2))
    check("Duplicate dropped (1 variant remains, not 2)",
          len(v2) == 1, f"got {len(v2)}")
    check("count_of_Genomic_Variants updated to match",
          env2["Genomic_Variant_umbrella"].get("count_of_Genomic_Variants") == 1,
          str(env2["Genomic_Variant_umbrella"].get("count_of_Genomic_Variants")))

    # ──── [3] DIFFERENT VARIANTS path — same gene, different change ──
    print("\n[3] DIFFERENT-VARIANTS path — same gene, different amino_acid_change")
    env3 = {
        "Genomic_Variant_umbrella": {
            "count_of_Genomic_Variants": 2,
            "Genomic_Variants": [
                {"gene_studied": "KRAS", "amino_acid_change": "p.G12D",
                 "variant_allele_frequency": "9.8%", "page_number": 1},
                {"gene_studied": "KRAS", "amino_acid_change": "p.G12C",   # DIFFERENT change
                 "variant_allele_frequency": "12%", "page_number": 1},
            ],
        },
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"tested_biomarkers": []},
    }
    _run_linker(env3)
    sup3 = [L for L in env3["links"] if L.get("type") == "variant_superseded_by"]
    check("Different changes → no supersession (not the same entity)",
          len(sup3) == 0, str(sup3))
    check("Both records kept untouched",
          len(env3["Genomic_Variant_umbrella"]["Genomic_Variants"]) == 2)

    # ──── [4] FIELD-AGNOSTIC: differing field is `result`, not VAF ───
    print("\n[4] FIELD-AGNOSTIC path — differing field is `result`, not VAF")
    env4 = {
        "Genomic_Variant_umbrella": {
            "count_of_Genomic_Variants": 2,
            "Genomic_Variants": [
                {"gene_studied": "KRAS", "amino_acid_change": "p.G12D",
                 "result": "Detected", "method": "NGS", "page_number": 1},
                {"gene_studied": "KRAS", "amino_acid_change": "p.G12D",
                 "result": "Not Detected", "method": "NGS", "page_number": 6},   # ← differs on `result`
            ],
        },
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"tested_biomarkers": []},
    }
    _run_linker(env4)
    sup4 = [L for L in env4["links"] if L.get("type") == "variant_superseded_by"]
    check("Result mismatch triggers same supersession mechanism (no field-specific code)",
          len(sup4) == 1, str(sup4))
    if sup4:
        check("Winner is still higher_page_number (the page-6 record)",
              sup4[0].get("to_ref") == "Genomic_Variant_umbrella.Genomic_Variants[1]",
              str(sup4[0]))

    # ──── [5] BIOMARKER section — same machinery, different section ─
    print("\n[5] BIOMARKER section — proves the rule is generic across sections")
    env5 = {
        "Genomic_Variant_umbrella": {"Genomic_Variants": []},
        "tested_biomarker_umbrella": {"tested_biomarkers": []},
        "other_molecular_biomarker_umbrella": {
            "count_of_other_molecular_biomarkers": 2,
            "other_molecular_biomarkers": [
                {"biomarker_name": "TMB", "result": "12 mut/Mb", "page_number": 1},
                {"biomarker_name": "TMB", "result": "15 mut/Mb", "page_number": 6},
            ],
        },
    }
    _run_linker(env5)
    sup5 = [L for L in env5["links"] if L.get("type") == "variant_superseded_by"]
    check("Biomarker conflict triggers supersession (same generic machinery)",
          len(sup5) == 1, str(sup5))
    if sup5:
        prod5 = to_production(env5, mapping_path=mpath)["genomic_pathology_extraction"]
        pbm = prod5["other_molecular_biomarker_umbrella"]["other_molecular_biomarkers"]
        check("Production emits only the later-page biomarker (15 mut/Mb)",
              len(pbm) == 1 and pbm[0].get("result") == "15 mut/Mb",
              str([b.get("result") for b in pbm]))

    # ──── [6] WINNER POLICY edge: tie on page → smallest index wins ──
    print("\n[6] WINNER POLICY: tie on page_number → stable (smallest index wins)")
    env6 = _link_kras("9.8%", 6, "10%", 6)
    _run_linker(env6)
    sup6 = [L for L in env6["links"] if L.get("type") == "variant_superseded_by"]
    check("Tie produces ONE supersession link",
          len(sup6) == 1, str(sup6))
    if sup6:
        # max(page=6, page=6) tie-broken by smallest index → winner is index 0
        check("Tie winner is the lower-index record (stable ordering)",
              sup6[0].get("to_ref") == "Genomic_Variant_umbrella.Genomic_Variants[0]",
              str(sup6[0]))

    # ──── [7] UNRESOLVED IDENTITY: a record missing identity key → singleton ──
    print("\n[7] UNRESOLVED IDENTITY: missing identity_key field → no collapse")
    env7 = {
        "Genomic_Variant_umbrella": {
            "count_of_Genomic_Variants": 2,
            "Genomic_Variants": [
                {"gene_studied": "KRAS", "amino_acid_change": None,   # missing identity_key
                 "variant_allele_frequency": "9.8%", "page_number": 1},
                {"gene_studied": "KRAS", "amino_acid_change": "p.G12D",
                 "variant_allele_frequency": "10%", "page_number": 6},
            ],
        },
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"tested_biomarkers": []},
    }
    _run_linker(env7)
    sup7 = [L for L in env7["links"] if L.get("type") == "variant_superseded_by"]
    check("Unresolved identity → record stays separate; no supersession",
          len(sup7) == 0, str(sup7))
    check("Both records kept untouched",
          len(env7["Genomic_Variant_umbrella"]["Genomic_Variants"]) == 2)

    # ──── [7.5] NO BIDIRECTIONAL SEED LINKS for self-typed types ─────
    # Regression lock: the gene-key seed used to emit BOTH directions for
    # intra-section types (`variant_superseded_by`, `superseded_by`) because
    # it iterates all (i,j) where i!=j and gene matches. That produced
    # bidirectional links — the production filter then dropped BOTH records
    # in a same-gene pair, deleting (e.g.) KRAS entirely from production.
    # The fix: seed SKIPS self-typed types; `_apply_intra_section_dedup` is
    # the sole source of supersession links and emits ONE direction per group.
    print("\n[7.5] Seed pass MUST NOT emit bidirectional supersession links")
    env75 = _link_kras("9.8%", 1, "10%", 6)
    _run_linker(env75)
    sup75 = [L for L in env75["links"] if L.get("type") == "variant_superseded_by"]
    check("Exactly ONE supersession link per duplicate (not bidirectional)",
          len(sup75) == 1, str(sup75))
    if sup75:
        check("Direction is earlier-page → later-page",
              sup75[0].get("from_ref", "").endswith("[0]")
              and sup75[0].get("to_ref", "").endswith("[1]"),
              str(sup75[0]))

    # ──── [8] HGVS PREFIX INSENSITIVE — the real-world KRAS case ────
    print("\n[8] HGVS PREFIX-INSENSITIVE identity (the actual KRAS case from")
    print("    full_report_Redacted.pdf: page 1 = 'G12D', page 6 = 'p.G12D')")
    env8 = {
        "Genomic_Variant_umbrella": {
            "count_of_Genomic_Variants": 2,
            "Genomic_Variants": [
                {"gene_studied": "KRAS", "amino_acid_change": "G12D",       # no prefix
                 "variant_allele_frequency": "9.8%", "page_number": 1},
                {"gene_studied": "KRAS", "amino_acid_change": "p.G12D",     # p. prefix
                 "variant_allele_frequency": "10",  "page_number": 6},
            ],
        },
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"tested_biomarkers": []},
    }
    _run_linker(env8)
    sup8 = [L for L in env8["links"] if L.get("type") == "variant_superseded_by"]
    check("'G12D' and 'p.G12D' canonicalize to the SAME identity → supersession fires",
          len(sup8) == 1, str(sup8))
    if sup8:
        prod8 = to_production(env8, mapping_path=mpath)["genomic_pathology_extraction"]
        pg8 = prod8["Genomic_Variant_umbrella"]["Genomic_Variants"]
        check("Production emits only the later-page record",
              len(pg8) == 1 and pg8[0].get("page_number") == 6,
              f"len={len(pg8)} pages={[v.get('page_number') for v in pg8]}")

    print("-" * 60)
    if fails:
        print(f"VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("VERIFY: PASS — generic intra-section dedup (variants + biomarkers); "
          "supersession on any field difference; identical mentions collapse; "
          "HGVS prefix-insensitive identity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
