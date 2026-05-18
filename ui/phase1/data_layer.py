"""
data_layer — backend-aware run/extraction/artifact loaders for the UI.

Mirrors the Persistence backend selected by PERSISTENCE_BACKEND env:
  local    → reads from `local_runs/runs_v1.jsonl` + `extractions_v1.jsonl`
              + `artifacts/<doc_id>/*.json`
  bigquery → reads from BigQuery + GCS

All loaders are sync (Streamlit caches sync results well via @st.cache_data).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.persistence import load_storage_config

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    """One row from runs_v1 — what the sidebar table displays."""

    run_id: str
    doc_id: str
    pipeline_version: str
    verdict: str | None
    verdict_reason: str
    cost_usd: float
    latency_ms: int
    completed_at: str
    artifacts_prefix: str


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def active_backend() -> str:
    return (os.environ.get("PERSISTENCE_BACKEND") or "local").lower()


# ---------------------------------------------------------------------------
# Local-backend loaders
# ---------------------------------------------------------------------------


def _local_dir() -> Path:
    return load_storage_config("phase_1").local_dir_resolved


def list_runs_local(*, limit: int = 200) -> list[RunSummary]:
    """Read runs_v1.jsonl, return most-recent-first. Skips malformed lines."""
    path = _local_dir() / "runs_v1.jsonl"
    if not path.exists():
        return []
    runs: list[RunSummary] = []
    bad = 0
    with path.open(encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            try:
                runs.append(RunSummary(
                    run_id=row.get("run_id", ""),
                    doc_id=row.get("doc_id", ""),
                    pipeline_version=row.get("pipeline_version", ""),
                    verdict=row.get("verdict"),
                    verdict_reason=row.get("verdict_reason") or "",
                    cost_usd=float(row.get("cost_usd") or 0.0),
                    latency_ms=int(row.get("latency_ms") or 0),
                    completed_at=row.get("completed_at") or "",
                    artifacts_prefix=row.get("artifacts_gcs_prefix") or "",
                ))
            except (TypeError, ValueError) as exc:
                logger.warning("malformed run row on line %d: %s", ln, exc)
                bad += 1
    if bad:
        logger.warning("list_runs_local: skipped %d malformed line(s) in %s", bad, path)
    # Most recent first (jsonl append order ≈ chronological).
    return list(reversed(runs))[:limit]


def load_extraction_local(doc_id: str) -> dict[str, Any] | None:
    """Most-recent extraction row for `doc_id` from extractions_v1.jsonl.
    Returns the full envelope (`genomic_pathology_extraction` shape).
    Skips malformed lines instead of crashing."""
    path = _local_dir() / "extractions_v1.jsonl"
    if not path.exists():
        return None
    matches: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("doc_id") == doc_id:
                matches.append(row)
    if not matches:
        return None
    row = matches[-1]
    raw = row.get("genomic_pathology_extraction")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "extraction row %s has unparseable genomic_pathology_extraction: %s",
                row.get("extraction_id"), exc,
            )
            return None
    return raw


def load_artifact_local(doc_id: str, kind: str, ext: str = "json") -> Any | None:
    """Read a per-doc artifact from local_runs/artifacts/<doc_id>/<kind>.<ext>.

    On JSON parse failure (e.g. legacy artifact written before the proto→JSON
    fix), logs a warning and returns None instead of crashing the UI.
    """
    path = _local_dir() / "artifacts" / doc_id / f"{kind}.{ext}"
    if not path.exists():
        return None
    if ext == "json":
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning(
                "artifact %s is not valid JSON (%s) — likely written by a "
                "pre-fix pipeline run. Delete the file and re-run the pipeline "
                "to regenerate. Returning None for now.",
                path, exc,
            )
            return None
    if ext in ("txt", "log"):
        return path.read_text(encoding="utf-8")
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.warning("could not read artifact %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# BigQuery-backend loaders (used when PERSISTENCE_BACKEND=bigquery)
# ---------------------------------------------------------------------------


def list_runs_bigquery(*, limit: int = 200) -> list[RunSummary]:
    cfg = load_storage_config("phase_1")
    try:
        from google.cloud import bigquery
    except ImportError:
        return []
    client = bigquery.Client(project=cfg.bigquery_project)
    query = (
        f"SELECT run_id, doc_id, pipeline_version, verdict, verdict_reason, "
        f"cost_usd, latency_ms, completed_at, artifacts_gcs_prefix "
        f"FROM `{cfg.runs_table_fqn}` ORDER BY completed_at DESC LIMIT {limit}"
    )
    try:
        rows = list(client.query(query).result())
    except Exception as exc:
        logger.warning("BigQuery list_runs failed: %s — returning []", exc)
        return []
    return [
        RunSummary(
            run_id=r["run_id"], doc_id=r["doc_id"],
            pipeline_version=r["pipeline_version"],
            verdict=r["verdict"], verdict_reason=r["verdict_reason"] or "",
            cost_usd=float(r["cost_usd"] or 0.0),
            latency_ms=int(r["latency_ms"] or 0),
            completed_at=str(r["completed_at"]) if r["completed_at"] else "",
            artifacts_prefix=r["artifacts_gcs_prefix"] or "",
        )
        for r in rows
    ]


def load_extraction_bigquery(doc_id: str) -> dict[str, Any] | None:
    cfg = load_storage_config("phase_1")
    try:
        from google.cloud import bigquery
    except ImportError:
        return None
    client = bigquery.Client(project=cfg.bigquery_project)
    query = (
        f"SELECT genomic_pathology_extraction "
        f"FROM `{cfg.extractions_table_fqn}` "
        f"WHERE doc_id = @doc_id ORDER BY extracted_at DESC LIMIT 1"
    )
    job = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("doc_id", "STRING", doc_id)]
        ),
    )
    rows = list(job.result())
    if not rows:
        return None
    raw = rows[0]["genomic_pathology_extraction"]
    return json.loads(raw) if isinstance(raw, str) else raw


# ---------------------------------------------------------------------------
# Backend-agnostic public API
# ---------------------------------------------------------------------------


def list_runs(*, limit: int = 200) -> list[RunSummary]:
    if active_backend() == "bigquery":
        return list_runs_bigquery(limit=limit)
    return list_runs_local(limit=limit)


def load_extraction(doc_id: str) -> dict[str, Any] | None:
    if active_backend() == "bigquery":
        return load_extraction_bigquery(doc_id)
    return load_extraction_local(doc_id)


def load_artifact(doc_id: str, kind: str, ext: str = "json") -> Any | None:
    # Only local-backend artifact loading is implemented in Phase 1 UI.
    # BigQuery backend would fetch from GCS; not needed for local-first UX.
    return load_artifact_local(doc_id, kind, ext)


def load_ground_truth(doc_id: str) -> dict[str, Any] | None:
    """Read ground_truth/<doc_id>.json or ground_truth/phase1/<doc_id>.json."""
    repo_root = Path(__file__).resolve().parents[2]
    for path in (
        repo_root / "ground_truth" / f"{doc_id}.json",
        repo_root / "ground_truth" / "phase1" / f"{doc_id}.json",
    ):
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("genomic_pathology_extraction", {}).get("report_metadata")
    return None
