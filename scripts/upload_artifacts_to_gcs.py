"""
Upload local pipeline artifacts to a GCS bucket for the SME Review Portal.

Reads from                                       Writes to
─────────────────────────────────────            ──────────────────────────────────────────────────
local_runs/artifacts/<doc_id>/<file>    →        gs://<GCS_ARTIFACTS_BUCKET>/<GCS_ARTIFACTS_PREFIX><doc_id>/<file>

Same key layout the Flask UI (ui/app.py) expects in GCS mode, so what you
upload with this script is immediately readable by the portal.

CONFIGURATION — same env vars the UI reads (loaded from .env automatically):
  GCS_ARTIFACTS_BUCKET  — target bucket name (REQUIRED)
  GCS_ARTIFACTS_PREFIX  — key prefix inside the bucket. Default: "artifacts/".
  GOOGLE_APPLICATION_CREDENTIALS — service-account key (or use ADC).

USAGE
  # dry-run first (no bytes written)
  python -m scripts.upload_artifacts_to_gcs --dry-run

  # upload everything under local_runs/artifacts/
  python -m scripts.upload_artifacts_to_gcs

  # upload only one doc
  python -m scripts.upload_artifacts_to_gcs --doc demo

  # skip files that already exist in the bucket (idempotent re-runs)
  python -m scripts.upload_artifacts_to_gcs --skip-existing

  # override bucket / prefix without touching .env
  python -m scripts.upload_artifacts_to_gcs \\
      --bucket staging-artifacts \\
      --prefix runs/v4/

  # only upload the small JSON artifacts, skip large PDFs
  python -m scripts.upload_artifacts_to_gcs --skip-pdf

EXIT CODES
  0 — success (all files uploaded or intentionally skipped)
  1 — configuration error (bucket not set, credentials missing, etc.)
  2 — upload failure on one or more files (details in the summary)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

# Load `.env` before reading env-driven defaults below.
import core.env_loader  # noqa: F401

logger = logging.getLogger("upload_artifacts")


def _repo_root() -> Path:
    """scripts/upload_artifacts_to_gcs.py → repo root is one level up."""
    return Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# Discovery — what to upload
# ─────────────────────────────────────────────────────────────────────────────


def _discover_docs(artifacts_dir: Path, doc_filter: str | None) -> list[Path]:
    """Return the list of per-doc folders under `artifacts_dir`.

    A folder counts as a doc when it contains `extraction_v2.json` — the same
    rule the UI uses to list docs on `/api/docs`. Skips any folder without
    that file so we don't upload half-baked runs.

    `doc_filter` (if given) is treated as a substring match on the folder
    name. Matches the same substring resolution rule the eval CLI uses.
    """
    if not artifacts_dir.is_dir():
        logger.error("artifacts dir does not exist: %s", artifacts_dir)
        return []

    all_docs = sorted(d for d in artifacts_dir.iterdir()
                       if d.is_dir() and (d / "extraction_v2.json").is_file())
    if doc_filter is None:
        return all_docs
    needle = doc_filter.lower()
    filtered = [d for d in all_docs if needle in d.name.lower()]
    if not filtered:
        logger.error("no doc folder matched substring %r (available: %s)",
                     doc_filter, [d.name for d in all_docs])
    return filtered


def _files_in_doc(doc_dir: Path, skip_pdf: bool) -> Iterable[Path]:
    """Yield every file inside a doc folder (non-recursive — the pipeline
    doesn't create nested subdirs today). Optionally skip large PDFs."""
    for f in sorted(doc_dir.iterdir()):
        if not f.is_file():
            continue
        if skip_pdf and f.suffix.lower() == ".pdf":
            logger.debug("  [skip-pdf] %s", f.name)
            continue
        yield f


# ─────────────────────────────────────────────────────────────────────────────
# GCS interaction
# ─────────────────────────────────────────────────────────────────────────────


def _mime_for(path: Path) -> str:
    """Same MIME map as the UI so the browser gets correct Content-Type."""
    return {
        ".json": "application/json",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".pb": "application/octet-stream",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".md": "text/markdown",
    }.get(path.suffix.lower(), "application/octet-stream")


def _upload_file(bucket, local_path: Path, blob_name: str,
                  *, dry_run: bool, skip_existing: bool) -> str:
    """Upload one file. Returns one of 'uploaded' / 'skipped' / 'failed'."""
    if dry_run:
        logger.info("  [DRY-RUN] would upload %s → gs://%s/%s (%d bytes)",
                     local_path.name, bucket.name, blob_name, local_path.stat().st_size)
        return "uploaded"    # count as uploaded so summary reflects intent

    blob = bucket.blob(blob_name)

    if skip_existing and blob.exists():
        logger.info("  [skip-existing] gs://%s/%s already exists",
                     bucket.name, blob_name)
        return "skipped"

    try:
        blob.upload_from_filename(str(local_path), content_type=_mime_for(local_path))
        logger.info("  ✓ %s → gs://%s/%s (%d bytes)",
                     local_path.name, bucket.name, blob_name, local_path.stat().st_size)
        return "uploaded"
    except Exception as exc:  # noqa: BLE001 — reporting, not swallowing
        logger.error("  ✗ FAILED %s → gs://%s/%s: %s",
                      local_path.name, bucket.name, blob_name, exc)
        return "failed"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.upload_artifacts_to_gcs",
        description="Upload local pipeline artifacts to GCS for the SME Review Portal.",
    )
    ap.add_argument("--bucket", default=None,
                    help="GCS bucket name. Default: GCS_ARTIFACTS_BUCKET env var.")
    ap.add_argument("--prefix", default=None,
                    help="Key prefix inside the bucket. Default: GCS_ARTIFACTS_PREFIX env "
                         "var (or 'artifacts/' if unset).")
    ap.add_argument("--artifacts-dir", type=Path, default=None,
                    help="Local source dir. Default: <repo>/local_runs/artifacts/")
    ap.add_argument("--doc", default=None,
                    help="Upload ONE doc only. Substring match on folder name.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be uploaded; do not actually upload.")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip files that already exist in the bucket. Recommended "
                         "for idempotent re-runs.")
    ap.add_argument("--skip-pdf", action="store_true",
                    help="Skip PDF files (large — useful when you only want JSON).")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
    )

    # Resolve bucket + prefix (CLI arg > env var > default).
    bucket_name = (args.bucket or os.environ.get("GCS_ARTIFACTS_BUCKET", "")).strip()
    if not bucket_name:
        logger.error(
            "no bucket specified — set GCS_ARTIFACTS_BUCKET in .env or pass --bucket.\n"
            "  (leaving it unset is fine for local UI mode, but this script needs a target)"
        )
        return 1

    prefix = (args.prefix or os.environ.get("GCS_ARTIFACTS_PREFIX", "artifacts/")).strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    artifacts_dir = args.artifacts_dir or (_repo_root() / "local_runs" / "artifacts")

    # Import GCS client here (after CLI validated) so --help works without the SDK.
    try:
        from google.cloud import storage
    except ImportError:
        logger.error("google-cloud-storage not installed. Run: pip install google-cloud-storage")
        return 1

    if not args.dry_run:
        try:
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            if not bucket.exists():
                logger.error("bucket does not exist: gs://%s", bucket_name)
                return 1
        except Exception as exc:  # noqa: BLE001
            logger.error("could not connect to GCS: %s", exc)
            logger.error(
                "  check GOOGLE_APPLICATION_CREDENTIALS or `gcloud auth application-default login`"
            )
            return 1
    else:
        # Dry-run — no real client needed; make a stub that carries the name.
        class _StubBucket:
            def __init__(self, name): self.name = name
        bucket = _StubBucket(bucket_name)

    docs = _discover_docs(artifacts_dir, args.doc)
    if not docs:
        return 1

    logger.info("=" * 66)
    logger.info("  Source: %s", artifacts_dir)
    logger.info("  Target: gs://%s/%s", bucket_name, prefix)
    logger.info("  Docs:   %d  (%s)", len(docs), ", ".join(d.name for d in docs))
    logger.info("  Mode:   %s", "DRY-RUN" if args.dry_run else "REAL UPLOAD")
    if args.skip_existing:
        logger.info("          skip-existing = ON  (idempotent re-run)")
    if args.skip_pdf:
        logger.info("          skip-pdf      = ON")
    logger.info("=" * 66)

    counts = {"uploaded": 0, "skipped": 0, "failed": 0}
    for doc_dir in docs:
        logger.info("")
        logger.info("── %s ──", doc_dir.name)
        for local_path in _files_in_doc(doc_dir, args.skip_pdf):
            blob_name = f"{prefix}{doc_dir.name}/{local_path.name}"
            status = _upload_file(
                bucket, local_path, blob_name,
                dry_run=args.dry_run,
                skip_existing=args.skip_existing,
            )
            counts[status] += 1

    logger.info("")
    logger.info("=" * 66)
    logger.info("  Summary: uploaded=%d  skipped=%d  failed=%d",
                counts["uploaded"], counts["skipped"], counts["failed"])
    logger.info("=" * 66)

    return 2 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
