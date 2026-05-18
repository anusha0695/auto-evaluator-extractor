"""
SchemaLoader — the central schema-driven generator.

Reads `config/schemas/genomic_pathology_v2.json` once at process start, then:

1. Generates a **Pydantic model** for any umbrella section on demand. Used
   by:
   - `verification/schema_validator.py` to validate team outputs.
   - `agents/extractor.py` to bind Gemini's structured-output `response_schema`.

2. Exposes **field metadata** (name, type, description, `extraction_mode`,
   format, pattern, required) for any section. Used by:
   - `core/prompt_renderer.py` to inject the `fields` list into Jinja prompts.

3. Exposes the **raw JSON Schema** so Gemini's structured-output config can
   consume it directly (Vertex AI accepts a subset of OpenAPI 3 schema).

This module is the **only** place that interprets JSON Schema. If the schema
file changes (new field, new section, new extraction_mode value), no other
file needs to change — Pydantic models and prompt contexts both regenerate
automatically.

Type mapping (JSON Schema → Python):

    "string"                       → str
    "integer"                      → int
    "number"                       → float
    "boolean"                      → bool
    "array"                        → list[<items type>]
    "object"                       → BaseModel (recursive nested model)
    ["string", "null"]             → str | None
    ["integer", "null"]            → int | None
    enum                           → Literal[...]

`additionalProperties: false` on the schema is respected by emitting
Pydantic `model_config = ConfigDict(extra="forbid")`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, create_model

from core.errors import SchemaLoadError, SchemaValidationError
from core.observability import trace

logger = logging.getLogger(__name__)


ExtractionMode = Literal["VERBATIM", "DERIVED", "VERBATIM_OR_INFERRED"]
UmbrellaSection = Literal[
    "report_metadata",
    "Genomic_Variant_umbrella",
    "other_molecular_biomarker_umbrella",
    "tested_biomarker_umbrella",
]


@dataclass(frozen=True)
class FieldMeta:
    """Per-field metadata extracted from JSON Schema for prompt rendering."""

    name: str
    type: str                         # human-readable type label for Jinja
    required: bool
    description: str
    extraction_mode: ExtractionMode | None
    format: str | None                # JSON Schema `format` (e.g. "date")
    pattern: str | None               # JSON Schema `pattern` (e.g. "^[0-9]{10}$")


@dataclass(frozen=True)
class SchemaSection:
    """All the metadata `PromptRenderer` needs for one umbrella section."""

    name: UmbrellaSection
    description: str
    fields: list[FieldMeta]
    pydantic_model: type[BaseModel]
    raw_json_schema: dict[str, Any]


class SchemaLoader:
    """Loads `genomic_pathology_v2.json` and exposes per-section metadata.

    Boot once at process start (`SchemaLoader.from_path(...)`), then call
    `get_section(name)` whenever a Pydantic model / prompt context is needed.
    Models are generated lazily on first request and cached.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self._root = schema
        self._cache: dict[str, SchemaSection] = {}
        self._validate_root_shape()

    # -----------------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------------

    @classmethod
    @trace("core.schema_loader.from_path")
    def from_path(cls, path: str | Path) -> SchemaLoader:
        """Load a JSON Schema from disk."""
        p = Path(path)
        if not p.exists():
            raise SchemaLoadError(
                f"Schema file not found: {p}",
                retry_safe=False,
                context={"path": str(p)},
            )
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaLoadError(
                f"Schema file is not valid JSON: {exc}",
                retry_safe=False,
                context={"path": str(p), "json_error": str(exc)},
            ) from exc
        logger.info("SchemaLoader: loaded schema from %s", p)
        return cls(raw)

    def _validate_root_shape(self) -> None:
        """Sanity-check the loaded schema matches our v2 expectations."""
        if self._root.get("title") != "genomic_pathology_extraction":
            raise SchemaLoadError(
                "Schema root title is not 'genomic_pathology_extraction' — "
                f"got {self._root.get('title')!r}",
                retry_safe=False,
            )
        required = set(self._root.get("required", []))
        expected = {
            "count_of_extracted_objects",
            "report_metadata",
            "Genomic_Variant_umbrella",
            "other_molecular_biomarker_umbrella",
            "tested_biomarker_umbrella",
        }
        missing = expected - required
        if missing:
            raise SchemaLoadError(
                f"Schema root is missing required top-level sections: {sorted(missing)}",
                retry_safe=False,
            )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def list_sections(self) -> list[UmbrellaSection]:
        """Return the 4 umbrella section names in schema order."""
        return [
            "report_metadata",
            "Genomic_Variant_umbrella",
            "other_molecular_biomarker_umbrella",
            "tested_biomarker_umbrella",
        ]

    @trace("core.schema_loader.get_section")
    def get_section(self, name: UmbrellaSection) -> SchemaSection:
        """Return the SchemaSection for the named umbrella, generating its
        Pydantic model on first request."""
        if name not in get_args(UmbrellaSection):
            raise SchemaLoadError(
                f"Unknown schema section: {name!r}. Valid sections: {self.list_sections()}",
                retry_safe=False,
            )
        if name in self._cache:
            return self._cache[name]

        section_schema = self._root["properties"][name]
        section = SchemaSection(
            name=name,
            description=section_schema.get("description", ""),
            fields=self._extract_field_metadata(section_schema),
            pydantic_model=self._build_pydantic_model(name, section_schema),
            raw_json_schema=section_schema,
        )
        self._cache[name] = section
        return section

    @trace("core.schema_loader.validate")
    def validate(self, name: UmbrellaSection, candidate: dict[str, Any]) -> BaseModel:
        """Validate a candidate dict against the named section's Pydantic
        model. Raises SchemaValidationError with field-level details on
        failure; returns the parsed model on success."""
        from pydantic import ValidationError

        section = self.get_section(name)
        try:
            return section.pydantic_model.model_validate(candidate)
        except ValidationError as exc:
            raise SchemaValidationError(
                f"Schema validation failed for section {name!r}",
                retry_safe=False,
                context={"section": name, "errors": exc.errors()},
            ) from exc

    def get_root_schema(self) -> dict[str, Any]:
        """Return the raw JSON Schema for use as Gemini `response_schema`."""
        return self._root

    # -----------------------------------------------------------------------
    # Field metadata extraction (drives prompt rendering)
    # -----------------------------------------------------------------------

    def _extract_field_metadata(self, section_schema: dict[str, Any]) -> list[FieldMeta]:
        """Walk a section schema's `properties` and emit `FieldMeta` records."""
        required = set(section_schema.get("required", []))
        out: list[FieldMeta] = []
        for fname, fschema in section_schema.get("properties", {}).items():
            out.append(
                FieldMeta(
                    name=fname,
                    type=self._jsonschema_type_label(fschema),
                    required=fname in required,
                    description=fschema.get("description", ""),
                    extraction_mode=fschema.get("extraction_mode"),
                    format=fschema.get("format"),
                    pattern=fschema.get("pattern"),
                )
            )
        return out

    @staticmethod
    def _jsonschema_type_label(field_schema: dict[str, Any]) -> str:
        """Human-readable type label used in Jinja prompts (not the Python
        type — the prompt shows the JSON Schema shape for clarity)."""
        t = field_schema.get("type")
        if isinstance(t, list):
            return f"[{', '.join(repr(x) for x in t)}]"
        if t == "array":
            items = field_schema.get("items", {})
            return f"array of {SchemaLoader._jsonschema_type_label(items)}"
        if t == "object":
            return "object"
        return repr(t) if t else "object"

    # -----------------------------------------------------------------------
    # Pydantic model generation
    # -----------------------------------------------------------------------

    def _build_pydantic_model(
        self,
        model_name: str,
        section_schema: dict[str, Any],
    ) -> type[BaseModel]:
        """Generate a Pydantic model from a JSON Schema object subtree.

        Recursive — nested objects (e.g. each `Genomic_Variants[]` item)
        become their own generated submodels.
        """
        if section_schema.get("type") != "object":
            raise SchemaLoadError(
                f"_build_pydantic_model called on non-object schema: {model_name}",
                retry_safe=False,
            )

        required = set(section_schema.get("required", []))
        field_definitions: dict[str, Any] = {}

        for fname, fschema in section_schema.get("properties", {}).items():
            py_type = self._jsonschema_to_python_type(model_name, fname, fschema)
            default: Any = ... if fname in required else None
            field_definitions[fname] = (
                py_type,
                Field(default=default, description=fschema.get("description", "")),
            )

        # Respect additionalProperties: false → forbid extras.
        extra_policy = "forbid" if section_schema.get("additionalProperties") is False else "ignore"
        model_config = ConfigDict(extra=extra_policy)  # type: ignore[arg-type]

        model = create_model(
            self._safe_model_name(model_name),
            __config__=model_config,
            **field_definitions,
        )
        return model

    def _jsonschema_to_python_type(
        self,
        parent_name: str,
        field_name: str,
        field_schema: dict[str, Any],
    ) -> Any:
        """Map one JSON Schema field definition to a Python type annotation."""
        t = field_schema.get("type")

        # Union types like ["string", "null"]
        if isinstance(t, list):
            non_null = [x for x in t if x != "null"]
            nullable = "null" in t
            if len(non_null) == 1:
                base = self._primitive_type(non_null[0])
            else:
                # rare in v2 — fall back to Any | None
                base = Any
            return (base | None) if nullable else base

        if t == "array":
            items = field_schema.get("items", {})
            inner = self._jsonschema_to_python_type(parent_name, f"{field_name}_item", items)
            return list[inner]  # type: ignore[valid-type]

        if t == "object":
            # Recurse — generate a nested model.
            nested_name = f"{parent_name}__{field_name}"
            return self._build_pydantic_model(nested_name, field_schema)

        if t in {"string", "integer", "number", "boolean"}:
            return self._primitive_type(t)

        # Unknown / missing type — accept anything (validator will catch
        # downstream issues).
        return Any

    @staticmethod
    def _primitive_type(jsonschema_type: str) -> Any:
        return {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
        }.get(jsonschema_type, Any)

    @staticmethod
    def _safe_model_name(name: str) -> str:
        """Convert a schema section path into a valid Python class name."""
        return "".join(part.capitalize() for part in name.replace("__", "_").split("_"))
