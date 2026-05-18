"""
Typed exception family for the extractor pipeline.

Every error carries `doc_id` (the document being processed when it raised) and
a `retry_safe` flag so the runner / decision router can decide whether to
re-attempt or escalate to SME.

Hierarchy:

    PipelineError                            (abstract root)
    ├── PreprocessingError                   (raised inside preprocess/)
    │   ├── DocAIError
    │   ├── BlockProfilerError
    │   └── MedicalNERError
    ├── SchemaError
    │   ├── SchemaLoadError
    │   └── SchemaValidationError
    ├── PromptRenderError
    ├── AgentError
    └── ToolError
"""

from __future__ import annotations

from typing import Any


class PipelineError(Exception):
    """Root of the extractor's exception hierarchy.

    All pipeline-raised errors must subclass this. Catching `PipelineError`
    is the supported way for a runner / node / agent boundary to handle
    pipeline failures without swallowing unrelated Python exceptions.

    Attributes:
        doc_id: ID of the document being processed when the error was raised.
            May be `None` for errors raised outside a document context (e.g.
            schema-load failures at boot time).
        retry_safe: True if the operation that raised may be safely retried
            with the same inputs. False if the failure indicates a problem
            that won't go away on retry (corrupted PDF, schema mismatch).
        context: Free-form dict for additional diagnostic info, included in
            structured logs and OpenTelemetry span attributes.
    """

    def __init__(
        self,
        message: str,
        *,
        doc_id: str | None = None,
        retry_safe: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.doc_id = doc_id
        self.retry_safe = retry_safe
        self.context: dict[str, Any] = context or {}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"message={self.args[0]!r}, "
            f"doc_id={self.doc_id!r}, "
            f"retry_safe={self.retry_safe}, "
            f"context={self.context!r}"
            ")"
        )


# ---------------------------------------------------------------------------
# Preprocessing errors
# ---------------------------------------------------------------------------


class PreprocessingError(PipelineError):
    """Base class for any failure in the preprocess/ stage.

    All-or-nothing preprocessing: if any of DocAI / fax_filter / Block Profiler
    / Medical NER fails, the document is routed to SME rather than continuing
    with partial state.
    """


class DocAIError(PreprocessingError):
    """DocAI Layout Parser failed (network, quota, or processor returned an
    error response). Usually `retry_safe=True`."""


class BlockProfilerError(PreprocessingError):
    """Gemini Block Profiler call failed or returned an unparseable response."""


class MedicalNERError(PreprocessingError):
    """In-process SciSpaCy / MedSpaCy pipeline failed to load or run."""


# ---------------------------------------------------------------------------
# Schema errors
# ---------------------------------------------------------------------------


class SchemaError(PipelineError):
    """Base class for schema-related failures."""


class SchemaLoadError(SchemaError):
    """`genomic_pathology_v2.json` failed to load or could not be parsed.

    Always `retry_safe=False` — won't fix itself on retry.
    """


class SchemaValidationError(SchemaError):
    """A team output failed Pydantic schema validation. Carries `errors` (the
    list of Pydantic field errors) in `context`."""


# ---------------------------------------------------------------------------
# Prompt / agent / tool errors
# ---------------------------------------------------------------------------


class PromptRenderError(PipelineError):
    """A Jinja template failed to render — usually a missing context variable
    or a typo in the template. Always `retry_safe=False`."""


class AgentError(PipelineError):
    """An agent (Extractor / Coverage Auditor / Arbiter / Planner / VMAW /
    Linking) failed at the Gemini layer or emitted malformed structured
    output."""


class ToolError(PipelineError):
    """A LangChain tool invocation raised. Carries the tool name in `context`."""
