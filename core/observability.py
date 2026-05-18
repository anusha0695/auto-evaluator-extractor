"""
OpenTelemetry → Cloud Trace + Cloud Logging + Cloud Monitoring.

We deliberately avoid LangSmith — PHI must stay in the GCP project. This
module is the only place that touches OpenTelemetry directly; everything
else uses the `@trace` decorator or the `span(...)` context manager.

Boot model:

    >>> from core.observability import ObservabilityManager
    >>> ObservabilityManager.init_from_env()    # idempotent

After `init_from_env()` the SDK is configured globally; subsequent imports
of `trace` / `span` work without further setup. If
`CLOUD_TRACE_ENABLED=false` in the environment, spans become no-ops — useful
for unit tests and local dry-runs.

Span attributes set at the runner boundary:

    pipeline.doc_id
    pipeline.version
    pipeline.gcs_uri

These propagate to every child span so Cloud Trace queries by doc_id work.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# ---------------------------------------------------------------------------
# Lazy / optional imports — OTel + GCP exporters are runtime-optional so unit
# tests can run in a hermetic env without the GCP SDKs installed.
# ---------------------------------------------------------------------------

_otel_initialized = False
_otel_enabled = False
_tracer: Any = None


class ObservabilityManager:
    """Idempotent global boot for OpenTelemetry → Cloud Trace.

    Reads from environment variables (see `.env.example`):

        CLOUD_TRACE_ENABLED       — "true" / "false"
        CLOUD_LOGGING_ENABLED     — "true" / "false"
        CLOUD_MONITORING_ENABLED  — "true" / "false"
        OTEL_SERVICE_NAME         — service.name resource attribute
        OTEL_RESOURCE_ATTRIBUTES  — comma-separated key=value pairs
        GCP_PROJECT_ID            — destination project for trace export
    """

    @staticmethod
    def init_from_env() -> None:
        """Initialize OTel + GCP exporters. Safe to call multiple times.

        Cloud Trace is **off by default** for local dev — set
        `CLOUD_TRACE_ENABLED=true` in `.env` only when the GCP service
        account has `roles/cloudtrace.agent`. Otherwise span exports
        produce `PermissionDenied 403` noise without any real benefit.
        """
        global _otel_initialized, _otel_enabled, _tracer
        if _otel_initialized:
            return
        _otel_initialized = True

        trace_enabled = os.getenv("CLOUD_TRACE_ENABLED", "false").lower() == "true"
        if not trace_enabled:
            logger.debug(
                "ObservabilityManager: CLOUD_TRACE_ENABLED is not 'true' — "
                "spans are no-ops (set CLOUD_TRACE_ENABLED=true in .env to enable)"
            )
            _otel_enabled = False
            _tracer = None
            return

        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            try:
                from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            except ImportError as exc:
                logger.warning(
                    "opentelemetry-exporter-gcp-trace not installed (%s) — spans are no-ops",
                    exc,
                )
                _otel_enabled = False
                _tracer = None
                return

            # Wrapped exporter that auto-disables itself on PermissionDenied.
            # Without this, every span batch retries forever, flooding stderr
            # when the service account lacks roles/cloudtrace.agent.
            class _PermSafeExporter(CloudTraceSpanExporter):  # type: ignore[misc]
                _disabled = False

                def export(self_inner, spans):  # type: ignore[no-self-argument]
                    if _PermSafeExporter._disabled:
                        from opentelemetry.sdk.trace.export import SpanExportResult
                        return SpanExportResult.SUCCESS    # silently drop
                    try:
                        return super().export(spans)
                    except Exception as exc:
                        msg = str(exc)
                        if "PermissionDenied" in msg or "permission" in msg.lower():
                            _PermSafeExporter._disabled = True
                            logger.warning(
                                "Cloud Trace export PermissionDenied — disabling "
                                "exporter for the rest of this process. Grant "
                                "roles/cloudtrace.agent to the service account, "
                                "or set CLOUD_TRACE_ENABLED=false in .env to silence."
                            )
                            from opentelemetry.sdk.trace.export import SpanExportResult
                            return SpanExportResult.SUCCESS
                        raise

            project_id = os.getenv("GCP_PROJECT_ID")
            if not project_id:
                logger.warning("ObservabilityManager: GCP_PROJECT_ID unset — spans are no-ops")
                _otel_enabled = False
                _tracer = None
                return

            resource_attrs: dict[str, str] = {
                "service.name": os.getenv("OTEL_SERVICE_NAME", "extractor-pipeline"),
            }
            for kv in os.getenv("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    resource_attrs[k.strip()] = v.strip()

            provider = TracerProvider(resource=Resource.create(resource_attrs))
            provider.add_span_processor(
                BatchSpanProcessor(_PermSafeExporter(project_id=project_id))
            )
            otel_trace.set_tracer_provider(provider)

            # OTel's base CloudTraceSpanExporter.export() catches the
            # underlying exception internally and logs "Error while writing
            # to Cloud Trace" before returning FAILURE — so our wrapper's
            # `except` clause never sees it. Attach a once-only filter to
            # the OTel cloud_trace logger so the user sees one helpful
            # message instead of the same multi-line traceback every batch.
            _attach_once_only_filter()

            _tracer = otel_trace.get_tracer("extractor")
            _otel_enabled = True
            logger.info(
                "ObservabilityManager: Cloud Trace exporter enabled for project %s", project_id
            )

        except Exception:
            logger.exception("ObservabilityManager: failed to initialize OTel — spans are no-ops")
            _otel_enabled = False
            _tracer = None


def _attach_once_only_filter() -> None:
    """Attach a logging filter to the OTel cloud_trace logger that emits
    the first ERROR with a helpful summary and silences all subsequent
    ones for the rest of the process."""

    class _OnceOnly(logging.Filter):
        emitted = False

        def filter(self, record: logging.LogRecord) -> bool:
            if record.levelno < logging.ERROR:
                return True
            if _OnceOnly.emitted:
                return False
            _OnceOnly.emitted = True
            # Replace the noisy multi-line traceback message with a concise
            # actionable hint. Strip the exc_info so the stdlib doesn't print
            # the underlying traceback below the message.
            record.msg = (
                "Cloud Trace span export failed (likely PermissionDenied). "
                "Suppressing further errors from opentelemetry.exporter.cloud_trace "
                "for the rest of this process. To silence permanently: either grant "
                "roles/cloudtrace.agent to the service account, or set "
                "CLOUD_TRACE_ENABLED=false in .env."
            )
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            return True

    logging.getLogger("opentelemetry.exporter.cloud_trace").addFilter(_OnceOnly())


def _ensure_initialized() -> None:
    if not _otel_initialized:
        ObservabilityManager.init_from_env()


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """Open an OpenTelemetry span. No-op when tracing is disabled.

    Usage:

        with span("preprocess.docai.parse", {"doc_id": doc_id}) as s:
            ...
            s.set_attribute("docai.response_bytes", n_bytes)
    """
    _ensure_initialized()
    if not _otel_enabled or _tracer is None:
        # No-op span — yield a dummy object that absorbs attribute calls.
        yield _NoopSpan()
        return

    with _tracer.start_as_current_span(name) as otel_span:
        if attributes:
            for k, v in attributes.items():
                otel_span.set_attribute(k, v)
        yield otel_span


def trace(name: str | None = None) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that wraps a function call in an OpenTelemetry span.

    Span name defaults to `f"{func.__module__}.{func.__qualname__}"`. The
    decorator is async-aware: if `func` is a coroutine, an async wrapper
    is returned; otherwise a sync wrapper.

    Usage:

        @trace()
        def parse(self, gcs_uri: str) -> tuple[DocProfile, str]:
            ...

        @trace("agents.extractor.invoke")
        async def invoke(self, state, scope):
            ...
    """
    import asyncio

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        span_name = name or f"{func.__module__}.{func.__qualname__}"

        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                with span(span_name):
                    return await func(*args, **kwargs)  # type: ignore[no-any-return,misc]
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(span_name):
                return func(*args, **kwargs)

        return sync_wrapper

    return decorator


class _NoopSpan:
    """Stand-in span object when OTel is disabled."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        pass

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass
