"""
Convert a doc's extraction → production schema and save it.

Usage:
  PYTHONPATH=. python scripts/to_production.py --doc demo
  PYTHONPATH=. python scripts/to_production.py --doc demo --raw   # ignore SME-reviewed copy

Reads local_runs/artifacts/<doc>/extraction_reviewed.json (preferred — SME edits)
or extraction_v2.json; writes extraction_production.json next to them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from transform.to_production import to_production

_ROOT = Path(__file__).resolve().parent.parent
_ARTIFACTS = _ROOT / "local_runs" / "artifacts"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True, help="doc_id (artifact folder name)")
    ap.add_argument("--raw", action="store_true", help="use extraction_v2.json even if a reviewed copy exists")
    args = ap.parse_args(argv or sys.argv[1:])

    d = _ARTIFACTS / args.doc
    reviewed = d / "extraction_reviewed.json"
    base = d / "extraction_v2.json"
    src = base if args.raw else (reviewed if reviewed.exists() else base)
    if not src.exists():
        print(f"ERROR: no extraction for '{args.doc}' at {src}", file=sys.stderr)
        return 1

    envelope = json.loads(src.read_text(encoding="utf-8"))
    out = to_production(envelope)
    dest = d / "extraction_production.json"
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"wrote {dest}")
    if "genomic_pathology_extraction" in out:
        # identity_v4 (default): mCODE root, schema-completed sections
        inner = out["genomic_pathology_extraction"]
        print(f"  source: {src.name} · mode=identity_v4 · sections={len(inner)} "
              f"· keys={sorted(inner)}")
    elif "pathology_extraction" in out:
        # legacy pathology_extraction 5-section shape
        pe = out["pathology_extraction"]
        nbm = pe["pathology_biomarkers_findings"]["number_of_biomarkers_with_definitive_results"]
        nsp = len(pe["significant_findings"]["specimen_findings"])
        print(f"  source: {src.name} · mode=pathology_extraction "
              f"· biomarkers_with_result={nbm} · specimen_findings={nsp}")
    else:
        print(f"  source: {src.name} · root keys={sorted(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
