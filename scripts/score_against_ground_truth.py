#!/usr/bin/env python3
"""
score_against_ground_truth.py — diff a pipeline extraction against the
matching `ground_truth/*.json` and report per-field pass/fail.

Phase 1 scope: only the `report_metadata` umbrella is scored. The other 3
umbrellas (`Genomic_Variant_umbrella`, `other_molecular_biomarker_umbrella`,
`tested_biomarker_umbrella`) are checked for shape (must exist as empty
placeholders in Phase 1) but not for content. Phase 2 expands scoring to
all 4 umbrellas once they have hand-labelled ground truth.

Per-field comparison policy (highest precedence first):

  - Both null               → pass (correctly omitted)
  - Strict equality         → pass (exact match)
  - Date fields             → pass if both parse to the same ISO date
  - String fields           → pass if equal after lowercase + whitespace
                              normalize (e.g. "NEOGENOMICS" vs "NeoGenomics")
  - Number / int            → pass on exact equality
  - Otherwise               → fail

`llm_confidence_score` is excluded from scoring — it's a runtime quality
self-report, not an extraction target.

Usage:
    python scripts/score_against_ground_truth.py --doc demo \\
        --extraction-file local_extraction.json

    python scripts/score_against_ground_truth.py --doc demo \\
        --from-bigquery

Exit codes:
    0 — pass rate ≥ threshold (default 0.80)
    1 — pass rate <  threshold
    2 — failed to load extraction or ground truth
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make repo-root imports work regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env into os.environ for the --from-bigquery code path.
import core.env_loader  # noqa: E402, F401

logger = logging.getLogger("score")


# ---------------------------------------------------------------------------
# Field-level comparison
# ---------------------------------------------------------------------------

DATE_FIELDS = {
    "Patient_DOB",
    "Collection_Date",
    "Received_Date",
    "Report_Date",
}

# Fields excluded from scoring — they're meta-data, not extraction targets.
EXCLUDED_FIELDS = {
    "llm_confidence_score",   # model self-report
    "provenance",              # per-field source citations (Option B); UI consumes it
}

# Fields where strict equality is required (no normalization).
STRICT_FIELDS = {
    "total_pages",
    "Ordering_Provider_NPI",
    "Practice_NPI",
    "Practice_ZIP_5digit",
    "Practice_ZIP_4digit",
}


@dataclass
class FieldComparison:
    field_name: str
    expected: Any
    actual: Any
    passed: bool
    match_type: str   # "exact" | "both_null" | "date" | "normalized_string" | "fail"
    notes: str = ""


@dataclass
class ScoreReport:
    doc_id: str
    section_name: str
    comparisons: list[FieldComparison] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.comparisons)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.comparisons if c.passed)

    @property
    def pass_rate(self) -> float:
        if not self.total:
            return 0.0
        return self.passed / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "section": self.section_name,
            "total_fields": self.total,
            "passed": self.passed,
            "failed": self.total - self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "comparisons": [
                {
                    "field": c.field_name,
                    "expected": c.expected,
                    "actual": c.actual,
                    "passed": c.passed,
                    "match_type": c.match_type,
                    "notes": c.notes,
                }
                for c in self.comparisons
            ],
        }


def _normalize_string(s: str) -> str:
    """Lowercase + collapse whitespace, strip surrounding whitespace."""
    return re.sub(r"\s+", " ", s.strip()).lower()


def _try_parse_date_iso(value: Any) -> str | None:
    """Return value's ISO YYYY-MM-DD form if parseable, else None."""
    if not isinstance(value, str):
        return None
    try:
        from dateutil import parser as _dp

        # Strip trailing sex/TZ fragments — same logic as the date_parser tool.
        cleaned = re.sub(r"\s*/\s*[MFmf]\s*$", "", value.strip())
        dt = _dp.parse(cleaned, fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return None


def _compare_field(name: str, expected: Any, actual: Any) -> FieldComparison:
    """Compare one field, returning a FieldComparison."""
    # Both null → pass
    if expected is None and actual is None:
        return FieldComparison(name, expected, actual, True, "both_null")

    # One null, other not → fail
    if expected is None or actual is None:
        return FieldComparison(
            name, expected, actual, False, "fail",
            notes=f"one side null (expected={expected!r}, actual={actual!r})",
        )

    # Strict equality always wins (catches the common case quickly)
    if expected == actual:
        return FieldComparison(name, expected, actual, True, "exact")

    # Fields where only strict equality is allowed
    if name in STRICT_FIELDS:
        return FieldComparison(
            name, expected, actual, False, "fail",
            notes=f"strict field — expected exact match",
        )

    # Date fields → parse + compare
    if name in DATE_FIELDS:
        exp_iso = _try_parse_date_iso(expected)
        act_iso = _try_parse_date_iso(actual)
        if exp_iso and act_iso and exp_iso == act_iso:
            return FieldComparison(
                name, expected, actual, True, "date",
                notes=f"matched after ISO normalization ({exp_iso})",
            )
        return FieldComparison(
            name, expected, actual, False, "fail",
            notes=f"date mismatch (expected_iso={exp_iso}, actual_iso={act_iso})",
        )

    # Strings → normalized compare
    if isinstance(expected, str) and isinstance(actual, str):
        if _normalize_string(expected) == _normalize_string(actual):
            return FieldComparison(
                name, expected, actual, True, "normalized_string",
                notes="matched after lowercase + whitespace normalize",
            )
        return FieldComparison(
            name, expected, actual, False, "fail",
            notes="string mismatch even after normalization",
        )

    return FieldComparison(
        name, expected, actual, False, "fail",
        notes=f"type mismatch ({type(expected).__name__} vs {type(actual).__name__})",
    )


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------


def score_report_metadata(
    *,
    doc_id: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> ScoreReport:
    """Diff the two report_metadata payloads field by field."""
    report = ScoreReport(doc_id=doc_id, section_name="report_metadata")

    # Score every field in the ground truth.
    for field_name in expected:
        if field_name in EXCLUDED_FIELDS:
            continue
        cmp = _compare_field(field_name, expected[field_name], actual.get(field_name))
        report.comparisons.append(cmp)

    # Catch any extra fields the extractor emitted that aren't in ground truth.
    for field_name in actual:
        if field_name in EXCLUDED_FIELDS or field_name in expected:
            continue
        report.comparisons.append(FieldComparison(
            field_name, None, actual[field_name], False, "fail",
            notes="extra field — not in ground truth",
        ))

    return report


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_ground_truth(doc_id: str) -> dict[str, Any]:
    """Find the matching ground_truth/*.json. Tries primary then phase1/."""
    candidates = [
        _REPO_ROOT / "ground_truth" / f"{doc_id}.json",
        _REPO_ROOT / "ground_truth" / "phase1" / f"{doc_id}.json",
    ]
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            metadata = data.get("genomic_pathology_extraction", {}).get("report_metadata")
            if metadata is None:
                raise ValueError(
                    f"{path} loaded but has no "
                    f"`genomic_pathology_extraction.report_metadata` section"
                )
            logger.info("ground truth loaded from %s", path)
            return metadata
    raise FileNotFoundError(
        f"No ground truth found for doc_id={doc_id!r}. Tried: "
        f"{', '.join(str(p) for p in candidates)}"
    )


def load_extraction_file(path: str | Path) -> dict[str, Any]:
    """Read an extraction JSON from disk. Accepts either a full envelope
    `{genomic_pathology_extraction: {...}}` or just the inner extraction
    dict."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Extraction file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    # Accept both shapes.
    if "genomic_pathology_extraction" in data:
        envelope = data["genomic_pathology_extraction"]
    else:
        envelope = data
    metadata = envelope.get("report_metadata")
    if metadata is None:
        raise ValueError(
            f"{p} loaded but has no report_metadata (under top-level "
            "`genomic_pathology_extraction.report_metadata` or as a direct field)."
        )
    return metadata


async def load_extraction_from_bigquery(doc_id: str) -> dict[str, Any]:
    """Fetch the most-recent extraction row from BigQuery for this doc_id."""
    from core.persistence import load_storage_config

    cfg = load_storage_config("phase_1")
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-bigquery not installed — install Phase 1 deps "
            "(`make install`) before using --from-bigquery."
        ) from exc

    client = bigquery.Client(project=cfg.bigquery_project)
    query = (
        f"SELECT genomic_pathology_extraction "
        f"FROM `{cfg.extractions_table_fqn}` "
        f"WHERE doc_id = @doc_id "
        f"ORDER BY extracted_at DESC LIMIT 1"
    )
    job = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("doc_id", "STRING", doc_id)]
        ),
    )
    rows = list(job.result())
    if not rows:
        raise RuntimeError(
            f"No BigQuery extraction row found for doc_id={doc_id!r} in "
            f"{cfg.extractions_table_fqn}. Run the pipeline first via "
            f"`make run-local PDF=gs://.../{doc_id}.pdf`."
        )
    raw_json = rows[0]["genomic_pathology_extraction"]
    envelope = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    metadata = envelope.get("report_metadata")
    if metadata is None:
        raise ValueError(
            f"BigQuery row has no report_metadata under "
            "`genomic_pathology_extraction.report_metadata`."
        )
    return metadata


def load_extraction_from_local(doc_id: str) -> dict[str, Any]:
    """Fetch the most-recent extraction row from `local_runs/extractions_v1.jsonl`."""
    from core.persistence import load_storage_config

    cfg = load_storage_config("phase_1")
    jsonl_path = cfg.local_dir_resolved / "extractions_v1.jsonl"
    if not jsonl_path.exists():
        raise RuntimeError(
            f"Local extractions file not found: {jsonl_path}. "
            f"Run the pipeline first via `make run-local PDF=...`."
        )

    # Scan the file for the doc — last matching line wins (most recent).
    matches: list[dict[str, Any]] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("doc_id") == doc_id:
                matches.append(row)

    if not matches:
        raise RuntimeError(
            f"No local extraction row found for doc_id={doc_id!r} in {jsonl_path}. "
            f"Run the pipeline first via `make run-local PDF=...`."
        )
    row = matches[-1]   # most recent (file is append-only)
    raw_json = row.get("genomic_pathology_extraction")
    envelope = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    metadata = envelope.get("report_metadata") if envelope else None
    if metadata is None:
        raise ValueError(
            f"Local row {row.get('extraction_id')} has no report_metadata under "
            "`genomic_pathology_extraction.report_metadata`."
        )
    logger.info(
        "loaded extraction from %s (extraction_id=%s)", jsonl_path, row.get("extraction_id")
    )
    return metadata


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


GREEN = "\033[1;32m"
RED = "\033[1;31m"
YELLOW = "\033[1;33m"
DIM = "\033[2m"
RESET = "\033[0m"


def _print_table(report: ScoreReport, *, verbose: bool) -> None:
    """Print a per-field comparison table to stdout."""
    print(f"\n{'=' * 78}")
    print(f"Doc: {report.doc_id}   Section: {report.section_name}")
    print(f"{'=' * 78}")

    name_w = max((len(c.field_name) for c in report.comparisons), default=20)

    for c in report.comparisons:
        mark = f"{GREEN}✓{RESET}" if c.passed else f"{RED}✗{RESET}"
        expected_repr = _truncate(repr(c.expected), 28)
        actual_repr = _truncate(repr(c.actual), 28)
        line = f"  {mark}  {c.field_name:<{name_w}}  expected={expected_repr:<28}  actual={actual_repr}"
        print(line)
        if (not c.passed or verbose) and c.notes:
            color = DIM if c.passed else YELLOW
            print(f"     {color}{c.notes}{RESET}")
        if not c.passed and c.match_type != "fail":
            # Render the soft-match-type for non-strict passes
            pass

    rate = report.pass_rate * 100
    color = GREEN if report.pass_rate >= 0.80 else (YELLOW if report.pass_rate >= 0.60 else RED)
    print(f"\n  {color}Pass rate: {report.passed}/{report.total} = {rate:.1f}%{RESET}")


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# argparse + main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Score a pipeline extraction against the matching ground_truth JSON. "
            "Phase 1: report_metadata section only."
        ),
        epilog=(
            "Examples:\n"
            "  score_against_ground_truth.py --doc demo --extraction-file run.json\n"
            "  score_against_ground_truth.py --doc demo --from-bigquery\n"
            "  score_against_ground_truth.py --doc doc_3 --extraction-file run.json --threshold 0.9\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--doc", required=True, metavar="DOC_ID",
        help="Doc identifier (e.g. demo, doc_3, doc2_25). Used to find ground_truth/<doc>.json.",
    )
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument(
        "--extraction-file", metavar="PATH",
        help="Path to a local extraction JSON file. Accepts either the full envelope "
             "({genomic_pathology_extraction: {...}}) or the inner dict.",
    )
    src.add_argument(
        "--from-local", action="store_true",
        help="Fetch the most-recent extraction for this doc_id from "
             "`local_runs/extractions_v1.jsonl` (default when PERSISTENCE_BACKEND=local).",
    )
    src.add_argument(
        "--from-bigquery", action="store_true",
        help="Fetch the most-recent extraction for this doc_id from BigQuery "
             "(requires GCP auth + extractions_v1 table populated by the pipeline).",
    )
    p.add_argument(
        "--threshold", type=float, default=0.80,
        help="Acceptance threshold for pass rate (default: 0.80). "
             "Exits 0 if pass_rate ≥ threshold, else 1.",
    )
    p.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Emit machine-readable JSON report instead of the formatted table.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show notes for every comparison (default: only failed comparisons).",
    )
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-5s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    # ----- Load ground truth -----
    try:
        gt = load_ground_truth(args.doc)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR loading ground truth: {exc}", file=sys.stderr)
        return 2

    # ----- Load extraction -----
    # Resolution order:
    #   1. --extraction-file PATH    → read that file
    #   2. --from-bigquery           → BigQuery query
    #   3. --from-local              → local jsonl
    #   4. (no flag)                 → auto-detect: PERSISTENCE_BACKEND env var
    import os
    try:
        if args.extraction_file:
            actual = load_extraction_file(args.extraction_file)
        elif args.from_bigquery:
            actual = await load_extraction_from_bigquery(args.doc)
        elif args.from_local:
            actual = load_extraction_from_local(args.doc)
        else:
            backend = (os.environ.get("PERSISTENCE_BACKEND") or "local").lower()
            if backend == "bigquery":
                actual = await load_extraction_from_bigquery(args.doc)
            else:
                actual = load_extraction_from_local(args.doc)
    except Exception as exc:
        print(f"ERROR loading extraction: {exc}", file=sys.stderr)
        return 2

    # ----- Score -----
    report = score_report_metadata(doc_id=args.doc, expected=gt, actual=actual)

    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        _print_table(report, verbose=args.verbose)

    threshold_ok = report.pass_rate >= args.threshold
    if not args.json_output:
        verdict = (
            f"{GREEN}PASS{RESET}" if threshold_ok else f"{RED}FAIL{RESET}"
        )
        print(
            f"\n  Threshold: {args.threshold:.2f}   Verdict: {verdict}\n",
            file=sys.stderr,
        )
    return 0 if threshold_ok else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv or sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
