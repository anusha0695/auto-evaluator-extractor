"""
Arbiter — gated single LLM call (Gemini 2.5 Flash @ T=0.0).

Fires *only* when the CoverageAuditor disagreed with the Extractor. Picks one
of three resolution policies:

    ACCEPT_EXTRACTOR — Auditor's concerns are noise. Extractor's output stands.
    RE_EXTRACT       — A clear, addressable miss. Extractor re-runs with hints.
    INVOKE_VMAW      — Unresolvable from inputs alone. Phase 1 maps this to
                        an SME flag because VMAW ships in Phase 3.

Output is small + structured (no tools). Flash is sufficient — this is a
3-way classification, not extraction.

Phase 1 caller pseudocode (see teams/metadata_team.py):

    if auditor_result.coverage_ok and not auditor_result.gap_signal:
        return extractor_result       # commit, no Arbiter call
    arb = Arbiter(...)
    decision = await arb.invoke(
        doc_id=..., extractor_output=..., auditor_output=...
    )
    if decision.policy == "ACCEPT_EXTRACTOR":
        return extractor_result
    if decision.policy == "RE_EXTRACT":
        return await extractor.invoke(state=..., re_extract_hints=decision.re_extract_hints)
    if decision.policy == "INVOKE_VMAW":
        return  flag_to_sme(reason=decision.vmaw_dispatch_brief)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.base import Agent, AgentResult
from core.observability import trace as otel_trace
from core.prompt_renderer import PromptRenderer
from core.schema_loader import UmbrellaSection

logger = logging.getLogger(__name__)


ArbiterPolicy = Literal["ACCEPT_EXTRACTOR", "RE_EXTRACT", "INVOKE_VMAW"]


# ---------------------------------------------------------------------------
# Structured output schemas (mirror arbiter.j2 output spec)
# ---------------------------------------------------------------------------


class _ReExtractHint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_name: str
    hint: str


class _VmawBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disputed_field: str = ""
    extractor_claim: str = ""
    auditor_claim: str = ""
    question_for_vmaw: str = ""


class ArbiterOutput(BaseModel):
    """The full JSON the arbiter prompt asks Gemini Flash to emit."""

    model_config = ConfigDict(extra="forbid")
    policy: ArbiterPolicy
    reasoning: str = ""
    re_extract_hints: list[_ReExtractHint] = Field(default_factory=list)
    vmaw_dispatch_brief: _VmawBrief | None = None


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ArbiterResult(AgentResult):
    policy: ArbiterPolicy = "ACCEPT_EXTRACTOR"
    reasoning: str = ""
    re_extract_hints: list[dict[str, str]] = field(default_factory=list)
    vmaw_dispatch_brief: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Arbiter
# ---------------------------------------------------------------------------


class Arbiter(Agent):
    """Gated arbiter. Picks the resolution policy."""

    role = "arbiter"

    def __init__(
        self,
        *,
        team_name: str,
        schema_section: UmbrellaSection,
        prompt_renderer: PromptRenderer,
        pipeline_version: str = "v1",
        vmaw_available: bool = False,
        agent_id: str | None = None,
        model_name: str | None = None,
        temperature: float | None = None,
    ) -> None:
        # Arbiter defaults to Flash (cheap), unlike Extractor / Auditor which default to Pro.
        super().__init__(
            agent_id=agent_id or f"{team_name}.arbiter",
            model_name=model_name or self._default_flash_model(),
            temperature=temperature,
        )
        self._team_name = team_name
        self._schema_section = schema_section
        self._prompt_renderer = prompt_renderer
        self._pipeline_version = pipeline_version
        self._vmaw_available = vmaw_available

    @staticmethod
    def _default_flash_model() -> str:
        import os
        return os.environ.get("GEMINI_FLASH_MODEL", "gemini-2.5-flash")

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @otel_trace("agents.arbiter.invoke")
    async def invoke(
        self,
        *,
        doc_id: str,
        extractor_output: dict[str, Any],
        auditor_output: dict[str, Any],
    ) -> ArbiterResult:
        """Pick the resolution policy for the conflict between Extractor and Auditor."""
        t0 = time.monotonic()

        prompt = self._prompt_renderer.render_arbiter_prompt(
            team_name=self._team_name,
            schema_section=self._schema_section,
            extractor_output=extractor_output,
            auditor_report=auditor_output,
            pipeline_version=self._pipeline_version,
            vmaw_available=self._vmaw_available,
        )

        llm_out = await self._invoke_structured(prompt, response_model=ArbiterOutput)

        # If the Arbiter chose INVOKE_VMAW but VMAW isn't available in this
        # phase, convert it on the caller side (we just surface the policy as
        # the model picked it — caller maps it to SME flag for Phase 1).
        if llm_out.policy == "INVOKE_VMAW" and not self._vmaw_available:
            logger.info(
                "Arbiter %s: chose INVOKE_VMAW but vmaw_available=False in this phase. "
                "Caller will route to SME.",
                self.agent_id,
            )

        result = ArbiterResult(
            agent_id=self.agent_id,
            model_name=self.model_name,
            temperature=self.temperature,
            policy=llm_out.policy,
            reasoning=llm_out.reasoning,
            re_extract_hints=[
                {"field_name": h.field_name, "hint": h.hint}
                for h in llm_out.re_extract_hints
            ],
            vmaw_dispatch_brief=(
                {
                    "disputed_field": llm_out.vmaw_dispatch_brief.disputed_field,
                    "extractor_claim": llm_out.vmaw_dispatch_brief.extractor_claim,
                    "auditor_claim": llm_out.vmaw_dispatch_brief.auditor_claim,
                    "question_for_vmaw": llm_out.vmaw_dispatch_brief.question_for_vmaw,
                }
                if llm_out.vmaw_dispatch_brief is not None else None
            ),
        )
        result.latency_ms = int((time.monotonic() - t0) * 1000)

        result.reasoning_trace.append({
            "role": "ai",
            "content": (
                f"policy={result.policy} "
                f"hints={len(result.re_extract_hints)} "
                f"vmaw_brief={'yes' if result.vmaw_dispatch_brief else 'no'}: "
                f"{result.reasoning[:200]}"
            ),
        })

        logger.info(
            "Arbiter %s: doc_id=%s policy=%s (%dms)",
            self.agent_id, doc_id, result.policy, result.latency_ms,
        )
        return result
