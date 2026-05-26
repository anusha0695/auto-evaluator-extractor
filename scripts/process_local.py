#!/usr/bin/env python3
"""
process_local.py — local CLI for invoking the pipeline.

Two modes:

  Single doc:
      python scripts/process_local.py \
          --gcs-uri gs://patient_clinical_trial/patient_profiles/demo.pdf

  Batch (every PDF under a GCS prefix):
      python scripts/process_local.py \
          --list-bucket gs://patient_clinical_trial/patient_profiles/

Either mode dispatches to `pipeline.runner.run()` and prints a one-line
RunResult per doc. Designed for direct invocation, for `make run-local`,
and as the basis for `scripts/deploy_dataflow.py` (Phase 4).

Exit codes:
  0 — every doc landed at `auto_accept`
  1 — at least one doc landed at `sme_flag` (no errors, but needs SME review)
  2 — at least one doc errored (pipeline failure, not an SME flag)

Logging:
  By default, INFO level to stderr. `--verbose` bumps to DEBUG. The
  pipeline itself logs through structlog → stdlib; this script's
  formatting is opinionated for terminal readability.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Make repo-root imports work regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env BEFORE any module that reads os.environ at import time.
import core.env_loader  # noqa: E402, F401

from core.gcs_client import GCSClient, GcsUri  # noqa: E402
from core.persistence import load_storage_config  # noqa: E402
from pipeline.runner import RunResult, run  # noqa: E402

logger = logging.getLogger("process_local")


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


VERDICT_COLOR = {
    "auto_accept": "\033[1;32m",   # green
    "fixable":     "\033[1;36m",   # cyan (Phase 2: refuted bind → Arbiter/SME)
    "sme_flag":    "\033[1;33m",   # yellow
    "errored":     "\033[1;31m",   # red
}
RESET = "\033[0m"


def _format_run_result(r: RunResult) -> str:
    color = VERDICT_COLOR.get(r.verdict or "", "")
    verdict = r.verdict or "(none)"
    line1 = (
        f"{color}{verdict:>11}{RESET}  doc_id={r.doc_id:<20} "
        f"latency={r.total_latency_ms:>6}ms  "
        f"run={r.bigquery_run_row_id or '-'}  "
        f"ext={r.bigquery_extraction_row_id or '-'}"
    )
    line2 = f"             reason: {r.verdict_reason[:200]}" if r.verdict_reason else ""
    line3 = f"             error : {r.error}" if r.error else ""
    return "\n".join(x for x in (line1, line2, line3) if x)


# ---------------------------------------------------------------------------
# Doc-id derivation
# ---------------------------------------------------------------------------


def _doc_id_from_uri(gcs_uri: str) -> str:
    """Derive a short doc_id from a gs://.../<name>.pdf URI."""
    parsed = GcsUri.parse(gcs_uri)
    stem = Path(parsed.object_path).stem    # demo.pdf → "demo"
    return stem or "unknown"


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def _process_single(
    gcs_uri: str | None = None,
    *,
    local_pdf: str | None = None,
    version: str,
    doc_id: str | None,
    json_output: bool,
) -> RunResult:
    if gcs_uri:
        did = doc_id or _doc_id_from_uri(gcs_uri)
        logger.info("Processing doc_id=%s  uri=%s", did, gcs_uri)
        result = await run(did, gcs_uri=gcs_uri, version=version)
    else:
        assert local_pdf is not None
        did = doc_id or Path(local_pdf).stem
        logger.info("Processing doc_id=%s  local_pdf=%s", did, local_pdf)
        result = await run(did, local_pdf_path=local_pdf, version=version)
    if json_output:
        print(json.dumps(_run_result_to_dict(result), indent=2, default=str))
    else:
        print(_format_run_result(result))
    return result


async def _process_batch(
    bucket_prefix: str,
    *,
    version: str,
    limit: int | None,
    json_output: bool,
) -> list[RunResult]:
    parsed = GcsUri.parse(bucket_prefix)
    client = GCSClient()
    uris = await client.list_pdfs(parsed.bucket, parsed.object_path)
    if limit is not None:
        uris = uris[:limit]

    logger.info("Batch: %d PDF(s) under %s", len(uris), bucket_prefix)
    if not uris:
        print(f"No PDFs found under {bucket_prefix}", file=sys.stderr)
        return []

    # Process sequentially — Phase 1 keeps things simple. Phase 4 (Dataflow)
    # parallelizes at scale.
    results: list[RunResult] = []
    for uri in uris:
        result = await _process_single(
            uri, version=version, doc_id=None, json_output=json_output,
        )
        results.append(result)
    return results


def _run_result_to_dict(r: RunResult) -> dict[str, Any]:
    """JSON-serializable summary for --json output."""
    return {
        "doc_id": r.doc_id,
        "gcs_uri": r.gcs_uri,
        "pipeline_version": r.pipeline_version,
        "verdict": r.verdict,
        "verdict_reason": r.verdict_reason,
        "artifacts_gcs_prefix": r.artifacts_gcs_prefix,
        "bigquery_run_row_id": r.bigquery_run_row_id,
        "bigquery_extraction_row_id": r.bigquery_extraction_row_id,
        "otel_trace_id": r.otel_trace_id,
        "total_latency_ms": r.total_latency_ms,
        "cost_usd": r.cost_usd,
        "error": r.error,
        # PHI-safe verifier summary: names, pass/fail, counts, notes, and error
        # LOCATIONS (field paths) only — never raw extracted values.
        "verifier_summary": _sanitized_verifier_summary(r),
        # The assembled v3 envelope — included so it can be inspected even when
        # the verdict isn't auto_accept (persistence only writes it on accept).
        "extraction": r.extraction,
    }


def _sanitized_verifier_summary(r: RunResult) -> dict[str, Any]:
    """Build a PHI-safe view of verifier scorecards + binding summary from the
    run's final_state. Drops error `msg` (may echo values); keeps `loc` paths."""
    fs = r.final_state or {}
    cards = []
    for s in (fs.get("verifier_scorecards") or []):
        cards.append({
            "verifier_name": s.get("verifier_name"),
            "passed": s.get("passed"),
            "notes": s.get("notes", ""),
            "error_locs": [e.get("loc") for e in (s.get("field_errors") or [])][:25],
            "cosmetic_locs": [e.get("loc") for e in (s.get("cosmetic_notes") or [])][:25],
        })
    return {
        "scorecards": cards,
        "binding_verifier": fs.get("binding_verifier"),
        "team_results": {
            k: {"verdict": v.get("verdict"),
                "llm_confidence_score": v.get("llm_confidence_score"),
                "needs_review_count": v.get("needs_review_count")}
            for k, v in (fs.get("team_results") or {}).items()
        },
    }


def _aggregate_exit_code(results: list[RunResult]) -> int:
    """0 if every doc auto_accept; 1 if any sme_flag/fixable; 2 if any errored."""
    if any(r.verdict == "errored" for r in results):
        return 2
    if any(r.verdict in ("sme_flag", "fixable") for r in results):
        return 1
    return 0


def _print_summary(results: list[RunResult]) -> None:
    if not results:
        print("\nNo documents processed.", file=sys.stderr)
        return
    by_verdict: dict[str, int] = {}
    total_latency = 0
    for r in results:
        by_verdict[r.verdict or "(none)"] = by_verdict.get(r.verdict or "(none)", 0) + 1
        total_latency += r.total_latency_ms
    print(
        f"\nProcessed {len(results)} doc(s) in {total_latency}ms total. Verdicts: "
        + ", ".join(f"{k}={v}" for k, v in sorted(by_verdict.items())),
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# argparse + main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the extractor pipeline on one or more PDFs in GCS.",
        epilog=(
            "Examples:\n"
            "  process_local.py --gcs-uri gs://patient_clinical_trial/patient_profiles/demo.pdf\n"
            "  process_local.py --list-bucket gs://patient_clinical_trial/patient_profiles/\n"
            "  process_local.py --list-bucket gs://patient_clinical_trial/patient_profiles/ --limit 3\n"
            "  process_local.py --gcs-uri gs://... --json > result.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--gcs-uri",
        metavar="URI",
        help="Process a single PDF at the given gs://bucket/path URI.",
    )
    mode.add_argument(
        "--local-pdf",
        metavar="PATH",
        help="Process a single local PDF file (bytes sent inline to DocAI; "
             "no GCS upload of the input). Output artifacts still go to GCS.",
    )
    mode.add_argument(
        "--list-bucket",
        metavar="PREFIX",
        help="Process every .pdf under the given gs://bucket/prefix/.",
    )
    p.add_argument(
        "--version", default="v1", choices=["v1", "v2", "v3"],
        help="Pipeline version (default: v1; v2 = Phase 2a genomic umbrellas + clinical; "
             "v3 = Phase 3 repair loop + VMAW + SME review artifacts).",
    )
    p.add_argument(
        "--doc-id", metavar="ID",
        help="Override the auto-derived doc_id (single-doc mode only).",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="In batch mode, process at most N PDFs.",
    )
    p.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Emit each RunResult as a JSON object (one per doc).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging.",
    )
    return p


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Quiet a few chatty libraries we don't care about at INFO.
    for noisy in ("urllib3", "httpx", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    # Pre-flight: storage.yaml must be loadable. Fail fast with a helpful
    # message rather than the runner blowing up halfway through.
    try:
        load_storage_config("phase_1")
    except Exception as exc:
        print(f"ERROR: failed to load config/storage.yaml: {exc}", file=sys.stderr)
        return 2

    try:
        if args.gcs_uri:
            r = await _process_single(
                gcs_uri=args.gcs_uri,
                version=args.version,
                doc_id=args.doc_id,
                json_output=args.json_output,
            )
            results = [r]
        elif args.local_pdf:
            r = await _process_single(
                local_pdf=args.local_pdf,
                version=args.version,
                doc_id=args.doc_id,
                json_output=args.json_output,
            )
            results = [r]
        else:
            results = await _process_batch(
                args.list_bucket,
                version=args.version,
                limit=args.limit,
                json_output=args.json_output,
            )
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130

    _print_summary(results)
    return _aggregate_exit_code(results)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv or sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
