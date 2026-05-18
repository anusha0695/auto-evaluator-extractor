"""
Agent — abstract base for the Extractor / Coverage Auditor / Arbiter / Planner
/ Linking / VMAW classes.

Cross-cutting responsibilities centralized here:

  - Holds model_name + temperature (locked at 0.0 by project policy).
  - Builds the Gemini chat client (via the unified `langchain-google-genai`
    wrapper that auto-selects Vertex AI vs Google AI Studio per the
    GOOGLE_GENAI_USE_VERTEXAI env flag).
  - Provides a shared `_invoke_structured()` helper that calls Gemini with
    `with_structured_output(<pydantic model>)` and surfaces any tool-call /
    JSON-parse error as a typed `AgentError`.
  - Wraps every concrete `invoke(...)` call in an OpenTelemetry span.

Concrete agents implement `async def invoke(...)` with their own signature
(Extractor takes state + parser_hypothesis_count; Auditor takes the
Extractor's output; Arbiter takes both).

`AgentResult` is the common reasoning-trace container — Phase 1 UI displays
it; Phase 4 SME review loop persists it for re-runs after human edits.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from core.errors import AgentError
from core.observability import trace as otel_trace

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass
class AgentResult:
    """Universal container for an agent's output + reasoning trace.

    Concrete agents wrap their typed outputs in subclasses (e.g.
    `ExtractorResult`) but the common fields below are always populated so
    the Phase 1 UI / Phase 4 SME loop can render and audit them.
    """

    agent_id: str
    model_name: str
    temperature: float
    # Free-form trace items: each is a dict, persisted to GCS for audit.
    reasoning_trace: list[dict[str, Any]] = field(default_factory=list)
    tool_calls_made: int = 0
    llm_confidence_score: float | None = None
    latency_ms: int = 0
    # Token usage (best-effort — populated when the LLM response includes it).
    tokens_input: int = 0
    tokens_output: int = 0
    # An agent may emit a non-fatal warning (e.g. "low-confidence vendor inference")
    # without raising — collect them here.
    warnings: list[str] = field(default_factory=list)


class Agent:
    """Abstract base. Concrete agents subclass and implement `invoke(...)`.

    The constructor establishes the LLM identity (model + temperature).
    Subclasses store their own dependencies (PromptRenderer, SchemaLoader,
    etc.) as private attributes — `Agent` itself stays small.
    """

    role: str = "agent"   # overridden by subclasses: "extractor", "coverage_auditor", etc.

    def __init__(
        self,
        *,
        agent_id: str,
        model_name: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.model_name = model_name or os.environ.get(
            "GEMINI_PRO_MODEL", "gemini-2.5-pro"
        )
        self.temperature = float(
            temperature if temperature is not None
            else os.environ.get("GEMINI_TEMPERATURE", "0.0")
        )
        if abs(self.temperature) > 1e-9:
            logger.warning(
                "Agent %s constructed with temperature=%s — project policy is 0.0. "
                "Override only with sign-off.",
                self.agent_id, self.temperature,
            )

    # -----------------------------------------------------------------------
    # LLM helpers (used by every concrete agent)
    # -----------------------------------------------------------------------

    def _make_llm(self) -> Any:
        """Construct a ChatGoogleGenerativeAI bound to this agent's model.

        Uses the unified `langchain-google-genai` wrapper — backend selected
        by GOOGLE_GENAI_USE_VERTEXAI (Vertex AI / Google AI Studio).
        Created on every call to keep the instance stateless and threadsafe
        (the underlying client pools connections).
        """
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=self.model_name,
            temperature=self.temperature,
        )

    @otel_trace("agents.base._invoke_structured")
    async def _invoke_structured(
        self,
        prompt: str,
        *,
        response_model: type[T],
        method: str = "function_calling",
    ) -> T:
        """Single-shot structured-output call. Raises AgentError on any failure.

        `method="function_calling"` is the safer choice for langchain-google-genai
        4.x — it forces the LLM to emit a tool-call that maps cleanly to the
        Pydantic model. Pure JSON-mode emission ("json_mode") is also supported
        but parses less reliably for nested schemas.
        """
        llm = self._make_llm()
        try:
            structured = llm.with_structured_output(response_model, method=method)
            result: Any = await structured.ainvoke(prompt)
        except Exception as exc:
            raise AgentError(
                f"{self.role} structured call failed: {exc}",
                retry_safe=True,
                context={"agent_id": self.agent_id, "model": self.model_name,
                          "error": str(exc)},
            ) from exc
        if isinstance(result, response_model):
            return result
        try:
            return response_model.model_validate(result)
        except Exception as exc:
            raise AgentError(
                f"{self.role} returned output that failed Pydantic validation: {exc}",
                retry_safe=True,
                context={"agent_id": self.agent_id, "raw": str(result)[:500]},
            ) from exc

    # -----------------------------------------------------------------------
    # Token / latency helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _extract_usage(ai_msg: Any) -> tuple[int, int]:
        """Best-effort token-usage extraction from a langchain AIMessage."""
        try:
            usage = getattr(ai_msg, "usage_metadata", None) or {}
            return int(usage.get("input_tokens", 0) or 0), int(
                usage.get("output_tokens", 0) or 0
            )
        except Exception:
            return 0, 0
