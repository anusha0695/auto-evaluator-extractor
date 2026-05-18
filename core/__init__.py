"""
extractor.core — schema-independent foundation.

Public API exposed by this package:

    PipelineState         — TypedDict for the LangGraph state object.
    SchemaLoader          — loads genomic_pathology_v2.json, generates Pydantic
                            models at runtime, exposes field metadata for prompts.
    PromptRenderer        — Jinja2 renderer that pulls schema context.
    ObservabilityManager  — OpenTelemetry → Cloud Trace + Logging + Monitoring.
    trace                 — decorator that wraps a function in an OTel span.
    span                  — context manager for ad-hoc spans.
    PreprocessingError    — typed exception family (importable from core.errors).
    GCSClient             — async wrapper for GCS list/read/write.
    GcsUri                — parsed gs:// URI dataclass.
    Persistence           — BigQuery + GCS writes tagged by pipeline_version.
    StorageConfig         — parsed entry from config/storage.yaml.
    load_storage_config   — read one phase block from storage.yaml.
    build_checkpointer    — factory returning a LangGraph BaseCheckpointSaver.
    build_tools_for_state — closure-bound LangChain tool factory.

Everything in this package is schema-independent — swapping
config/schemas/genomic_pathology_v2.json for a different schema requires
zero changes here.
"""

from core.checkpointer import build_checkpointer
from core.errors import (
    PipelineError,
    PreprocessingError,
    DocAIError,
    BlockProfilerError,
    MedicalNERError,
    SchemaError,
    SchemaLoadError,
    SchemaValidationError,
    PromptRenderError,
    AgentError,
    ToolError,
)
from core.gcs_client import GCSClient, GcsUri
from core.observability import ObservabilityManager, span, trace
from core.persistence import Persistence, StorageConfig, load_storage_config
from core.prompt_renderer import PromptRenderer, ToolDescriptor
from core.schema_loader import FieldMeta, SchemaLoader, SchemaSection
from core.state import PipelineState
from core.tool_registry import build_tools_for_state, descriptors_from_yaml

__all__ = [
    # state
    "PipelineState",
    # schema
    "SchemaLoader",
    "SchemaSection",
    "FieldMeta",
    # prompts
    "PromptRenderer",
    "ToolDescriptor",
    # observability
    "ObservabilityManager",
    "trace",
    "span",
    # I/O
    "GCSClient",
    "GcsUri",
    "Persistence",
    "StorageConfig",
    "load_storage_config",
    # checkpointer
    "build_checkpointer",
    # tools
    "build_tools_for_state",
    "descriptors_from_yaml",
    # errors
    "PipelineError",
    "PreprocessingError",
    "DocAIError",
    "BlockProfilerError",
    "MedicalNERError",
    "SchemaError",
    "SchemaLoadError",
    "SchemaValidationError",
    "PromptRenderError",
    "AgentError",
    "ToolError",
]
