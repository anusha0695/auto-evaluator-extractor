"""
Persistence — reads `config/storage.yaml`, writes runs / extractions /
artifacts to the configured destinations.

Three write surfaces:

1. **`write_run(state)`** — one row to BigQuery `runs_v1` per pipeline run.
   Carries doc_id, pipeline_version, verdict, cost, latency, OTel trace_id,
   verifier scorecards.
2. **`write_extraction(state)`** — one row to BigQuery `extractions_v1` per
   accepted extraction. Carries doc_id, pipeline_version, the full schema-v2
   envelope as a JSON blob.
3. **`write_artifact(state, kind, content)`** — one object to GCS under
   `{artifacts_gcs_prefix}/{doc_id}/{kind}.json` (or `.bin` for raw bytes).
   Kinds: `docai_raw`, `block_profiles`, `parser_hypothesis`,
   `agent_trace_<team>`, `audit_<team>`, etc.

All three writes tag the row/object with `doc_id` and `pipeline_version` so
queries by either dimension always work.

The BigQuery client (like the GCS client) is created lazily so unit tests
can import this module without GCP credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.errors import PipelineError
from core.gcs_client import GCSClient
from core.observability import trace
from core.state import PipelineState

logger = logging.getLogger(__name__)


PersistenceBackend = Literal["local", "bigquery"]


# ---------------------------------------------------------------------------
# Storage config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageConfig:
    """Parsed entry from `config/storage.yaml` for one pipeline version."""

    pipeline_version: str
    artifacts_gcs_bucket: str
    artifacts_gcs_prefix: str
    bigquery_project: str
    bigquery_dataset: str
    bigquery_extractions_table: str
    bigquery_runs_table: str
    firestore_database: str
    firestore_checkpoint_collection: str
    input_gcs_bucket: str
    input_gcs_prefix: str
    # Local-persistence destination. Used when PERSISTENCE_BACKEND=local.
    # Relative to the repo root; default `local_runs/`.
    local_persistence_dir: str = "local_runs"

    @property
    def extractions_table_fqn(self) -> str:
        return f"{self.bigquery_project}.{self.bigquery_dataset}.{self.bigquery_extractions_table}"

    @property
    def runs_table_fqn(self) -> str:
        return f"{self.bigquery_project}.{self.bigquery_dataset}.{self.bigquery_runs_table}"

    def artifact_uri(self, doc_id: str, kind: str, ext: str = "json") -> str:
        """Return the artifact's URI for the active backend (gs:// or file://)."""
        if _active_backend() == "local":
            return f"file://{Path(self.local_persistence_dir).resolve()}/artifacts/{doc_id}/{kind}.{ext}"
        prefix = self.artifacts_gcs_prefix.rstrip("/")
        return f"gs://{self.artifacts_gcs_bucket}/{prefix}/{doc_id}/{kind}.{ext}"

    def doc_prefix(self, doc_id: str) -> str:
        """Return the per-doc artifacts prefix for the active backend."""
        if _active_backend() == "local":
            return f"file://{Path(self.local_persistence_dir).resolve()}/artifacts/{doc_id}/"
        prefix = self.artifacts_gcs_prefix.rstrip("/")
        return f"gs://{self.artifacts_gcs_bucket}/{prefix}/{doc_id}/"

    @property
    def local_dir_resolved(self) -> Path:
        """Absolute path to the local persistence root."""
        p = Path(self.local_persistence_dir)
        if not p.is_absolute():
            p = p.resolve()
        return p


def _active_backend() -> PersistenceBackend:
    """Read PERSISTENCE_BACKEND env var, default 'local'."""
    raw = (os.environ.get("PERSISTENCE_BACKEND") or "local").lower()
    if raw not in ("local", "bigquery"):
        raise PipelineError(
            f"Unknown PERSISTENCE_BACKEND={raw!r}. Valid: 'local' | 'bigquery'.",
            retry_safe=False,
        )
    return raw  # type: ignore[return-value]


def load_storage_config(
    phase_key: str = "phase_1",
    path: str | Path = "config/storage.yaml",
) -> StorageConfig:
    """Load and parse one phase block from `storage.yaml`."""
    p = Path(path)
    if not p.exists():
        raise PipelineError(
            f"storage.yaml not found: {p}",
            retry_safe=False,
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if phase_key not in raw:
        raise PipelineError(
            f"storage.yaml missing block {phase_key!r}",
            retry_safe=False,
        )
    block = raw[phase_key]
    return StorageConfig(
        pipeline_version=block["pipeline_version"],
        artifacts_gcs_bucket=block["artifacts_gcs_bucket"],
        artifacts_gcs_prefix=block["artifacts_gcs_prefix"],
        bigquery_project=block["bigquery_project"],
        bigquery_dataset=block["bigquery_dataset"],
        bigquery_extractions_table=block["bigquery_extractions_table"],
        bigquery_runs_table=block["bigquery_runs_table"],
        firestore_database=block["firestore_database"],
        firestore_checkpoint_collection=block["firestore_checkpoint_collection"],
        input_gcs_bucket=block["input_gcs_bucket"],
        input_gcs_prefix=block["input_gcs_prefix"],
        local_persistence_dir=block.get("local_persistence_dir", "local_runs"),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class Persistence:
    """Reads `storage.yaml` at construction; writes runs + extractions + artifacts.

    Two backends selected by PERSISTENCE_BACKEND env var:

      - `local` (default) — runs/extractions to `local_runs/*.jsonl`,
        artifacts to `local_runs/artifacts/<doc_id>/<kind>.<ext>`. No GCS
        bucket or BigQuery dataset required. Fast iteration during dev.
      - `bigquery`        — runs/extractions to BigQuery, artifacts to GCS.
        Production destination.

    The two backends produce the **same row shapes** so flipping the env
    var doesn't change what downstream code sees.
    """

    def __init__(
        self,
        config: StorageConfig,
        *,
        gcs_client: GCSClient | None = None,
    ) -> None:
        self._cfg = config
        self._gcs = gcs_client or GCSClient()
        self._bq_client: Any = None
        self._bq_lock = asyncio.Lock()

    @property
    def config(self) -> StorageConfig:
        return self._cfg

    @property
    def backend(self) -> PersistenceBackend:
        return _active_backend()

    async def _get_bq_client(self) -> Any:
        if self._bq_client is None:
            async with self._bq_lock:
                if self._bq_client is None:
                    self._bq_client = await asyncio.to_thread(_make_bigquery_client)
        return self._bq_client

    # -----------------------------------------------------------------------
    # Artifact writes — local file OR GCS
    # -----------------------------------------------------------------------

    @trace("core.persistence.write_artifact")
    async def write_artifact(
        self,
        doc_id: str,
        kind: str,
        content: bytes | str | dict[str, Any],
        *,
        ext: str = "json",
    ) -> str:
        """Write a per-doc artifact. Returns the destination URI (gs:// or file://)."""
        if self.backend == "local":
            return await self._write_artifact_local(doc_id, kind, content, ext=ext)
        uri = self._cfg.artifact_uri(doc_id, kind, ext)
        await self._gcs.write(uri, content)
        logger.info("persistence: wrote artifact %s", uri)
        return uri

    async def _write_artifact_local(
        self, doc_id: str, kind: str, content: bytes | str | dict[str, Any] | list[Any],
        *, ext: str,
    ) -> str:
        path = self._cfg.local_dir_resolved / "artifacts" / doc_id / f"{kind}.{ext}"

        def _do_write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            elif isinstance(content, (dict, list)):
                # Important: lists must also go through json.dumps. The previous
                # version routed lists through `str(content)` which produced
                # Python repr (single quotes, `{'block_id': '1'}`) — not valid
                # JSON. Block profiles and parser hypothesis candidates are
                # passed as TypedDict lists, which is how that bug landed.
                path.write_text(
                    json.dumps(content, indent=2, default=str), encoding="utf-8"
                )
            elif isinstance(content, str):
                # Caller pre-serialized the content (e.g. DocAI does its own
                # `json.dumps()` before passing the string in). Write verbatim.
                path.write_text(content, encoding="utf-8")
            else:
                # Last resort — anything else gets json-dumped via default=str.
                path.write_text(
                    json.dumps(content, indent=2, default=str), encoding="utf-8"
                )

        await asyncio.to_thread(_do_write)
        uri = f"file://{path}"
        logger.info("persistence: wrote local artifact %s", uri)
        return uri

    # -----------------------------------------------------------------------
    # BigQuery writes
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Run + extraction writes — local jsonl OR BigQuery
    # -----------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    @trace("core.persistence.write_run")
    async def write_run(self, state: PipelineState) -> str:
        """Write one pipeline-run record. Returns its `run_id` (UUID4)."""
        run_id = str(uuid.uuid4())
        row = self._build_run_row(state, run_id)
        if self.backend == "local":
            await self._append_jsonl_local("runs_v1.jsonl", row)
        else:
            await self._bq_insert(self._cfg.runs_table_fqn, [row])
        logger.info(
            "persistence(%s): wrote run row %s for doc_id=%s",
            self.backend, run_id, state["doc_id"],
        )
        return run_id

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    @trace("core.persistence.write_extraction")
    async def write_extraction(
        self, state: PipelineState, *, run_id: str | None = None,
    ) -> str:
        """Write one extraction record with the full schema-v2 envelope."""
        extraction = state.get("extraction")
        if not extraction:
            raise PipelineError(
                "write_extraction called with empty state.extraction",
                retry_safe=False,
                doc_id=state.get("doc_id"),
            )

        extraction_id = str(uuid.uuid4())
        row = {
            "extraction_id": extraction_id,
            "run_id": run_id,
            "doc_id": state["doc_id"],
            "pipeline_version": state["pipeline_version"],
            "genomic_pathology_extraction": json.dumps(extraction, default=str),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.backend == "local":
            await self._append_jsonl_local("extractions_v1.jsonl", row)
        else:
            await self._bq_insert(self._cfg.extractions_table_fqn, [row])
        logger.info(
            "persistence(%s): wrote extraction row %s for doc_id=%s",
            self.backend, extraction_id, state["doc_id"],
        )
        return extraction_id

    def _build_run_row(self, state: PipelineState, run_id: str) -> dict[str, Any]:
        """Common row shape — same for local and BigQuery."""
        return {
            "run_id": run_id,
            "doc_id": state["doc_id"],
            "pipeline_version": state["pipeline_version"],
            "verdict": state.get("verdict"),
            "verdict_reason": state.get("verdict_reason"),
            "cost_usd": float(state.get("cost_usd", 0.0)),
            "latency_ms": int(state.get("latency_ms", 0)),
            "otel_trace_id": state.get("otel_trace_id"),
            "preprocessing_errors": json.dumps(state.get("preprocessing_errors", []), default=str),
            "verifier_scorecards": json.dumps(state.get("verifier_scorecards", []), default=str),
            "artifacts_gcs_prefix": state.get("artifacts_gcs_prefix")
                or self._cfg.doc_prefix(state["doc_id"]),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _append_jsonl_local(self, filename: str, row: dict[str, Any]) -> None:
        """Append one JSON-encoded line to `local_runs/<filename>`."""
        path = self._cfg.local_dir_resolved / filename

        def _do_append() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")

        await asyncio.to_thread(_do_append)

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    async def _bq_insert(self, table_fqn: str, rows: list[dict[str, Any]]) -> None:
        client = await self._get_bq_client()

        def _do_insert() -> list[Any]:
            errors = client.insert_rows_json(table_fqn, rows)
            return errors

        errors = await asyncio.to_thread(_do_insert)
        if errors:
            raise PipelineError(
                f"BigQuery insert into {table_fqn} reported errors",
                retry_safe=True,
                context={"table": table_fqn, "bq_errors": errors},
            )


# ---------------------------------------------------------------------------
# Client factory — isolated for testability
# ---------------------------------------------------------------------------


def _make_bigquery_client() -> Any:
    from google.cloud import bigquery

    return bigquery.Client()
