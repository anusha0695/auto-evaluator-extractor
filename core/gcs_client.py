"""
GCSClient — thin async wrapper around google-cloud-storage.

Two responsibilities:

1. **List + read** PDFs from `gs://patient_clinical_trial/patient_profiles/`
   — used by `scripts/process_local.py` and the preprocess layer.
2. **Write artifacts** to `gs://patient_clinical_trial/extraction_outputs/phase1/<doc_id>/`
   — raw DocAI responses, block_profiles JSON, parser_hypothesis JSON, agent
   traces. Used by `core/persistence.py` and the preprocess layer.

The Google client is synchronous; we wrap blocking calls in
`asyncio.to_thread()` so they don't block the LangGraph event loop. Retries
are tenacity-decorated with exponential backoff.

URI conventions:

    gs://<bucket>/<object_path>

Where `<object_path>` may contain `/` separators (GCS uses flat namespacing
but treats `/` as a folder boundary for prefix listing).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.errors import PipelineError
from core.observability import trace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lightweight URI helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GcsUri:
    """Parsed `gs://bucket/path` reference."""

    bucket: str
    object_path: str

    @classmethod
    def parse(cls, uri: str) -> GcsUri:
        if not uri.startswith("gs://"):
            raise ValueError(f"Not a GCS URI: {uri!r}")
        without_scheme = uri[len("gs://"):]
        if "/" not in without_scheme:
            return cls(bucket=without_scheme, object_path="")
        bucket, _, object_path = without_scheme.partition("/")
        return cls(bucket=bucket, object_path=object_path)

    def __str__(self) -> str:
        return f"gs://{self.bucket}/{self.object_path}".rstrip("/")


# ---------------------------------------------------------------------------
# GCSClient
# ---------------------------------------------------------------------------


class GCSClient:
    """Async wrapper around google-cloud-storage with tenacity retries.

    The underlying `google.cloud.storage.Client` is created lazily so importing
    this module doesn't require GCP credentials (useful for unit tests).
    """

    def __init__(self) -> None:
        self._client: Any = None  # google.cloud.storage.Client | None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = await asyncio.to_thread(_make_storage_client)
        return self._client

    # -----------------------------------------------------------------------
    # Read operations
    # -----------------------------------------------------------------------

    @trace("core.gcs_client.list_pdfs")
    async def list_pdfs(self, bucket: str, prefix: str) -> list[str]:
        """List every `.pdf` object under `gs://{bucket}/{prefix}`.

        Returns a list of fully-qualified `gs://...` URIs in lexical order.
        """
        client = await self._get_client()

        def _do_list() -> list[str]:
            uris: list[str] = []
            for blob in client.list_blobs(bucket, prefix=prefix):
                if blob.name.lower().endswith(".pdf"):
                    uris.append(f"gs://{bucket}/{blob.name}")
            return sorted(uris)

        return await asyncio.to_thread(_do_list)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    @trace("core.gcs_client.read_bytes")
    async def read_bytes(self, gcs_uri: str) -> bytes:
        """Read raw bytes from `gs://bucket/path`. Retries with backoff."""
        parsed = GcsUri.parse(gcs_uri)
        client = await self._get_client()

        def _do_read() -> bytes:
            bucket = client.bucket(parsed.bucket)
            blob = bucket.blob(parsed.object_path)
            return blob.download_as_bytes()

        try:
            return await asyncio.to_thread(_do_read)
        except Exception as exc:
            raise PipelineError(
                f"GCS read failed: {gcs_uri}",
                retry_safe=True,
                context={"uri": gcs_uri, "error": str(exc)},
            ) from exc

    @trace("core.gcs_client.list_pdfs_iter")
    async def iter_pdfs(self, bucket: str, prefix: str) -> AsyncIterator[str]:
        """Async iterator over PDF URIs (for very large prefixes)."""
        # google-cloud-storage's iterator is blocking; chunk into a list and
        # yield page-by-page.
        uris = await self.list_pdfs(bucket, prefix)
        for uri in uris:
            yield uri

    # -----------------------------------------------------------------------
    # Write operations
    # -----------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    @trace("core.gcs_client.write")
    async def write(
        self,
        gcs_uri: str,
        content: bytes | str | dict[str, Any],
        *,
        content_type: str | None = None,
    ) -> str:
        """Write content to `gs://bucket/path`. Auto-serializes dict as JSON.

        Returns the fully-qualified `gs://...` URI on success.
        """
        parsed = GcsUri.parse(gcs_uri)
        client = await self._get_client()

        if isinstance(content, dict):
            payload = json.dumps(content, indent=2, sort_keys=False, default=str).encode("utf-8")
            ct = content_type or "application/json"
        elif isinstance(content, str):
            payload = content.encode("utf-8")
            ct = content_type or "text/plain"
        elif isinstance(content, bytes):
            payload = content
            ct = content_type or "application/octet-stream"
        else:
            raise TypeError(f"Unsupported content type: {type(content)}")

        def _do_write() -> None:
            bucket = client.bucket(parsed.bucket)
            blob = bucket.blob(parsed.object_path)
            blob.upload_from_string(payload, content_type=ct)

        try:
            await asyncio.to_thread(_do_write)
        except Exception as exc:
            raise PipelineError(
                f"GCS write failed: {gcs_uri}",
                retry_safe=True,
                context={"uri": gcs_uri, "error": str(exc)},
            ) from exc

        return gcs_uri

    @trace("core.gcs_client.exists")
    async def exists(self, gcs_uri: str) -> bool:
        """True if the object exists at `gcs_uri`."""
        parsed = GcsUri.parse(gcs_uri)
        client = await self._get_client()

        def _do_check() -> bool:
            bucket = client.bucket(parsed.bucket)
            blob = bucket.blob(parsed.object_path)
            return blob.exists()

        return await asyncio.to_thread(_do_check)


# ---------------------------------------------------------------------------
# Client factory — isolated for testability
# ---------------------------------------------------------------------------


def _make_storage_client() -> Any:
    """Construct the underlying google-cloud-storage Client.

    Isolated as a module-level function so tests can monkeypatch it without
    needing real GCP credentials.
    """
    from google.cloud import storage

    return storage.Client()
