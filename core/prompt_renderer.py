"""
PromptRenderer — Jinja-based prompt renderer that pulls schema context.

Single boot at process start; render-by-name for every agent invocation.

Usage:

    from core.schema_loader import SchemaLoader
    from core.prompt_renderer import PromptRenderer

    schema = SchemaLoader.from_path("config/schemas/genomic_pathology_v2.json")
    renderer = PromptRenderer(schema_loader=schema)

    system_prompt = renderer.render_team_extractor_prompt(
        team_name="MetadataTeam",
        team_prompt_template="config/prompts/metadata_team.j2",
        schema_section="report_metadata",
        pipeline_version="v1",
        available_tools=tool_registry.tools_for_team("metadata_team"),
        parser_hypothesis_count=17,
    )

The renderer is **stateless** beyond its Jinja environment + schema loader
reference — every render call is self-contained, no per-call mutation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateError,
    select_autoescape,
)

from core.errors import PromptRenderError
from core.observability import trace
from core.schema_loader import SchemaLoader, UmbrellaSection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDescriptor:
    """Subset of a `config/tools.yaml` entry that the prompt needs."""

    name: str
    description: str
    input_schema: dict[str, Any]


class PromptRenderer:
    """Renders Jinja2 prompt templates with schema-driven context.

    Three render methods covering the three agent roles in Phase 1
    (Extractor, Coverage Auditor, Arbiter). Each accepts the team / agent
    inputs and produces a fully-rendered system-prompt string ready to
    pass to Gemini.
    """

    def __init__(
        self,
        *,
        schema_loader: SchemaLoader,
        prompts_root: str | Path = "config/prompts",
    ) -> None:
        self._schema_loader = schema_loader
        self._prompts_root = Path(prompts_root)

        if not self._prompts_root.exists():
            raise PromptRenderError(
                f"Prompts directory not found: {self._prompts_root}",
                retry_safe=False,
            )

        self._env = Environment(
            loader=FileSystemLoader(str(self._prompts_root)),
            autoescape=select_autoescape(default=False),
            undefined=StrictUndefined,   # fail loudly on missing context vars
            trim_blocks=False,
            lstrip_blocks=False,
            keep_trailing_newline=True,
        )
        logger.info("PromptRenderer initialized from %s", self._prompts_root.resolve())

    # -----------------------------------------------------------------------
    # Public render methods (one per agent role)
    # -----------------------------------------------------------------------

    @trace("core.prompt_renderer.render_team_extractor_prompt")
    def render_team_extractor_prompt(
        self,
        *,
        team_name: str,
        team_prompt_template: str,
        schema_section: UmbrellaSection,
        pipeline_version: str,
        available_tools: list[ToolDescriptor],
        parser_hypothesis_count: int = 0,
    ) -> str:
        """Render the Extractor's system prompt for a team.

        The team-specific template (e.g. `metadata_team.j2`) is expected to
        `{% include 'system/extractor.j2' %}` at the top, which gives the
        base ReAct + field-contract scaffolding before the team-specific
        rules are layered on.
        """
        section = self._schema_loader.get_section(schema_section)
        context = {
            "team_name": team_name,
            "pipeline_version": pipeline_version,
            "schema_section_name": section.name,
            "schema_section_description": section.description,
            "fields": [self._field_to_dict(f) for f in section.fields],
            "available_tools": [self._tool_to_dict(t) for t in available_tools],
            "parser_hypothesis_count": parser_hypothesis_count,
        }
        rel_template = self._make_relative_template_path(team_prompt_template)
        return self._render(rel_template, context)

    @trace("core.prompt_renderer.render_coverage_auditor_prompt")
    def render_coverage_auditor_prompt(
        self,
        *,
        team_name: str,
        schema_section: UmbrellaSection,
        extractor_output: dict[str, Any],
        parser_hypothesis: list[dict[str, Any]],
        coverage_gap_tolerance: float,
    ) -> str:
        """Render the Coverage Auditor's system prompt for one team's output."""
        section = self._schema_loader.get_section(schema_section)
        context = {
            "team_name": team_name,
            "schema_section_name": section.name,
            "schema_section_description": section.description,
            "fields": [self._field_to_dict(f) for f in section.fields],
            "extractor_output": extractor_output,
            "parser_hypothesis": parser_hypothesis,
            "parser_hypothesis_count": len(parser_hypothesis),
            "coverage_gap_tolerance": coverage_gap_tolerance,
        }
        return self._render("system/coverage_auditor.j2", context)

    @trace("core.prompt_renderer.render_arbiter_prompt")
    def render_arbiter_prompt(
        self,
        *,
        team_name: str,
        schema_section: UmbrellaSection,
        extractor_output: dict[str, Any],
        auditor_report: dict[str, Any],
        pipeline_version: str,
        vmaw_available: bool,
    ) -> str:
        """Render the Arbiter's system prompt (gated; only invoked on conflict)."""
        context = {
            "team_name": team_name,
            "schema_section_name": schema_section,
            "extractor_output": extractor_output,
            "auditor_report": auditor_report,
            "pipeline_version": pipeline_version,
            "vmaw_available": vmaw_available,
        }
        return self._render("system/arbiter.j2", context)

    # -----------------------------------------------------------------------
    # Preprocessing prompts (Block D consumers)
    # -----------------------------------------------------------------------

    @trace("core.prompt_renderer.render_block_profiler_prompt")
    def render_block_profiler_prompt(
        self,
        *,
        doc_id: str,
        total_pages: int,
        blocks: list[dict[str, Any]],
    ) -> str:
        context = {"doc_id": doc_id, "total_pages": total_pages, "blocks": blocks}
        return self._render("preprocess/block_profiler.j2", context)

    @trace("core.prompt_renderer.render_medical_ner_prompt")
    def render_medical_ner_prompt(
        self,
        *,
        doc_id: str,
        pages: list[dict[str, Any]],
        raw_scispacy_entities: list[dict[str, Any]],
    ) -> str:
        context = {
            "doc_id": doc_id,
            "pages": pages,
            "raw_scispacy_entities": raw_scispacy_entities,
        }
        return self._render("preprocess/medical_ner.j2", context)

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _render(self, relative_template_path: str, context: dict[str, Any]) -> str:
        try:
            template = self._env.get_template(relative_template_path)
            return template.render(**context)
        except TemplateError as exc:
            raise PromptRenderError(
                f"Failed to render {relative_template_path}: {exc}",
                retry_safe=False,
                context={"template": relative_template_path, "jinja_error": str(exc)},
            ) from exc

    def _make_relative_template_path(self, path: str) -> str:
        """Strip the `config/prompts/` prefix if present so the FileSystemLoader
        can resolve the template relative to its root."""
        prefix = "config/prompts/"
        if path.startswith(prefix):
            return path[len(prefix):]
        return path

    @staticmethod
    def _field_to_dict(field: Any) -> dict[str, Any]:
        return {
            "name": field.name,
            "type": field.type,
            "required": field.required,
            "description": field.description,
            "extraction_mode": field.extraction_mode,
            "format": field.format,
            "pattern": field.pattern,
        }

    @staticmethod
    def _tool_to_dict(tool: ToolDescriptor) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
