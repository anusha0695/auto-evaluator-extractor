"""
CoverageAuditor — single LLM call (Gemini 2.5 Pro @ T=0.0).

Different prompt from the Extractor (`coverage_auditor.j2`), no tools. Job:
review the Extractor's output and the parser_hypothesis side-by-side to flag:

  - missed_fields:               required schema fields the Extractor emitted
                                  as `null` but the source actually contains.
  - spurious_fields:              fields the Extractor emitted but look wrong.
  - parser_hypothesis_misses:     entities SciSpaCy / Gemini-NER candidate
                                  flagged but the Extractor ignored.
  - gap_signal (bool):            true if the gap between extracted-non-null
                                  count and parser-hypothesis count exceeds
                                  the team's `coverage_gap_tolerance` (from
                                  teams.yaml).

Output drives the MetadataTeam subgraph's branch:

    coverage_ok=true  AND  gap_signal=false  →  commit Extractor output
    otherwise                                  →  Arbiter decides resolution policy
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agents.base import Agent, AgentResult
from core.observability import trace as otel_trace
from core.prompt_renderer import PromptRenderer
from core.schema_loader import UmbrellaSection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured output schemas (mirror coverage_auditor.j2 output spec)
# ---------------------------------------------------------------------------


class MissedField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_name: str
    evidence_location: str = ""
    extracted_value_from_source: str = ""
    why_extractor_should_have_caught_it: str = ""


class SpuriousField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_name: str
    extractor_value: str = ""
    evidence_against: str = ""


class ParserHypothesisMiss(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_text: str
    candidate_label: str = ""
    candidate_page: int = 1
    should_have_landed_in: str = ""


class AuditorOutput(BaseModel):
    """The full JSON the auditor prompt asks Gemini to emit."""

    model_config = ConfigDict(extra="forbid")
    coverage_ok: bool
    gap_signal: bool
    missed_fields: list[MissedField] = Field(default_factory=list)
    spurious_fields: list[SpuriousField] = Field(default_factory=list)
    parser_hypothesis_misses: list[ParserHypothesisMiss] = Field(default_factory=list)
    auditor_notes: str = ""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class AuditorResult(AgentResult):
    coverage_ok: bool = True
    gap_signal: bool = False
    missed_fields: list[MissedField] = field(default_factory=list)
    spurious_fields: list[SpuriousField] = field(default_factory=list)
    parser_hypothesis_misses: list[ParserHypothesisMiss] = field(default_factory=list)
    auditor_notes: str = ""


# ---------------------------------------------------------------------------
# CoverageAuditor
# ---------------------------------------------------------------------------


class CoverageAuditor(Agent):
    """Recall safety net. Single LLM call; no tools.

    The Coverage Auditor optimizes for recall; the Extractor optimizes for
    precision. Their disagreement is what triggers the Arbiter.
    """

    role = "coverage_auditor"

    def __init__(
        self,
        *,
        team_name: str,
        schema_section: UmbrellaSection,
        prompt_renderer: PromptRenderer,
        coverage_gap_tolerance: float = 0.10,
        agent_id: str | None = None,
        model_name: str | None = None,
        temperature: float | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id or f"{team_name}.coverage_auditor",
            model_name=model_name,
            temperature=temperature,
        )
        self._team_name = team_name
        self._schema_section = schema_section
        self._prompt_renderer = prompt_renderer
        self._coverage_gap_tolerance = coverage_gap_tolerance

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @otel_trace("agents.coverage_auditor.invoke")
    async def invoke(
        self,
        *,
        doc_id: str,
        extractor_output: dict[str, Any],
        parser_hypothesis_candidates: list[dict[str, Any]],
    ) -> AuditorResult:
        """Run the auditor over the Extractor's output.

        `parser_hypothesis_candidates` should already be filtered to this
        team's umbrella (caller does the filter — keeps this class team-agnostic).
        """
        t0 = time.monotonic()

        prompt = self._prompt_renderer.render_coverage_auditor_prompt(
            team_name=self._team_name,
            schema_section=self._schema_section,
            extractor_output=extractor_output,
            parser_hypothesis=parser_hypothesis_candidates,
            coverage_gap_tolerance=self._coverage_gap_tolerance,
        )

        llm_out = await self._invoke_structured(prompt, response_model=AuditorOutput)

        result = AuditorResult(
            agent_id=self.agent_id,
            model_name=self.model_name,
            temperature=self.temperature,
            coverage_ok=llm_out.coverage_ok,
            gap_signal=llm_out.gap_signal,
            missed_fields=list(llm_out.missed_fields),
            spurious_fields=list(llm_out.spurious_fields),
            parser_hypothesis_misses=list(llm_out.parser_hypothesis_misses),
            auditor_notes=llm_out.auditor_notes,
        )
        result.latency_ms = int((time.monotonic() - t0) * 1000)

        # Capture the verdict + a short summary in the trace.
        result.reasoning_trace.append({
            "role": "ai",
            "content": (
                f"coverage_ok={llm_out.coverage_ok} gap_signal={llm_out.gap_signal} "
                f"missed={len(llm_out.missed_fields)} "
                f"spurious={len(llm_out.spurious_fields)} "
                f"hypothesis_misses={len(llm_out.parser_hypothesis_misses)}"
            ),
        })

        logger.info(
            "CoverageAuditor %s: doc_id=%s coverage_ok=%s gap_signal=%s "
            "missed=%d spurious=%d hyp_misses=%d (%dms)",
            self.agent_id, doc_id, result.coverage_ok, result.gap_signal,
            len(result.missed_fields), len(result.spurious_fields),
            len(result.parser_hypothesis_misses), result.latency_ms,
        )
        return result
