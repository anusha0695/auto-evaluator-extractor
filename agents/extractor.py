"""
Extractor — ReAct agent. Gemini 2.5 Pro @ T=0.0 + 7 tools + structured output.

ReAct loop (manually orchestrated, capped at MAX_ITERATIONS):

    Thought / Action(tool) → Tool result → Thought / Action(tool) → ...
                                       → (no more tool calls) → Final JSON

The "final JSON" is validated against the team's Pydantic model (generated
at runtime by core.schema_loader from genomic_pathology_v2.json). If the
LLM emits malformed JSON or fails schema validation, the loop retries once
with the error in context; second failure → AgentError.

Why a manual loop instead of langgraph.prebuilt.create_react_agent:
  - We need fine control over the tool-call → result handshake so we can
    persist the full Thought / Action / Observation trace for SME audit.
  - We want a *required* terminal step that emits structured output matching
    the team's schema section — not arbitrary text. langgraph's prebuilt
    treats "no tool call" as terminal regardless of content.
  - The loop is ~80 lines; the prebuilt would be a wrapper layer plus a
    post-parse step that ends up the same size.

Phase 1 wires this once for MetadataTeam (schema section `report_metadata`).
Phase 2 wires three more instances for the other 3 teams with their team-
specific prompt templates — the class body doesn't change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from agents.base import Agent, AgentResult
from core.errors import AgentError, SchemaValidationError
from core.observability import trace as otel_trace
from core.prompt_renderer import PromptRenderer, ToolDescriptor
from core.schema_loader import SchemaLoader, UmbrellaSection
from core.state import PipelineState
from core.tool_registry import build_tools_for_state, descriptors_from_yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ExtractorResult(AgentResult):
    """Extractor-specific result. Inherits AgentResult fields + adds output."""

    output: dict[str, Any] = field(default_factory=dict)   # validated team-section JSON
    iterations_used: int = 0
    final_answer_retry_count: int = 0


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


# Reasonable cap; metadata extraction typically converges in 3-6 iterations.
MAX_ITERATIONS: int = 12

# Pattern that grabs a JSON object from the end of the model's final
# message (sometimes the LLM wraps it in ```json fences despite the prompt
# saying not to). Defense in depth.
_FINAL_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```|(\{[\s\S]*\})", re.MULTILINE)


class Extractor(Agent):
    """ReAct agent emitting structured output for one schema-v2 umbrella section.

    One instance per team. The instance is stateless beyond construction —
    each `invoke(...)` call gets a fresh tool bundle bound to the current
    PipelineState, so concurrent docs don't interfere.
    """

    role = "extractor"

    def __init__(
        self,
        *,
        team_name: str,
        schema_section: UmbrellaSection,
        team_prompt_template: str,
        tool_allowlist: list[str],
        prompt_renderer: PromptRenderer,
        schema_loader: SchemaLoader,
        pipeline_version: str = "v1",
        tools_yaml_path: str = "config/tools.yaml",
        agent_id: str | None = None,
        model_name: str | None = None,
        temperature: float | None = None,
        max_iterations: int = MAX_ITERATIONS,
    ) -> None:
        super().__init__(
            agent_id=agent_id or f"{team_name}.extractor",
            model_name=model_name,
            temperature=temperature,
        )
        self._team_name = team_name
        self._schema_section = schema_section
        self._team_prompt_template = team_prompt_template
        self._tool_allowlist = tool_allowlist
        self._prompt_renderer = prompt_renderer
        self._schema_loader = schema_loader
        self._pipeline_version = pipeline_version
        self._tools_yaml_path = tools_yaml_path
        self._max_iterations = max_iterations

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @otel_trace("agents.extractor.invoke")
    async def invoke(
        self,
        *,
        state: PipelineState,
        parser_hypothesis_count: int = 0,
        re_extract_hints: list[dict[str, str]] | None = None,
    ) -> ExtractorResult:
        """Run the ReAct loop for the bound team. Returns a validated
        ExtractorResult.

        Args:
            state: current PipelineState — tools and the per-page text are
                bound to this state.
            parser_hypothesis_count: candidate count from
                state["parser_hypothesis"] filtered to this team's section.
                Surfaced in the prompt so the Auditor's gap_signal threshold
                lines up with what the Extractor thinks it should find.
            re_extract_hints: optional list of {field_name, hint} dicts from
                a previous Arbiter pass. Injected as a "Hints from the last
                attempt" preamble in the prompt.

        Returns:
            ExtractorResult with `output` validated against the team's
            Pydantic schema section.
        """
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )

        t0 = time.monotonic()
        result = ExtractorResult(
            agent_id=self.agent_id,
            model_name=self.model_name,
            temperature=self.temperature,
        )

        # ----- 1. Build the tools bound to this state -----------------------
        descriptors = descriptors_from_yaml(self._tools_yaml_path, allowlist=self._tool_allowlist)
        tools = build_tools_for_state(
            state=state, allowlist=self._tool_allowlist, schema_loader=self._schema_loader,
        )
        tools_by_name = {t.name: t for t in tools}

        # ----- 2. Render the system prompt ----------------------------------
        system_prompt = self._prompt_renderer.render_team_extractor_prompt(
            team_name=self._team_name,
            team_prompt_template=self._team_prompt_template,
            schema_section=self._schema_section,
            pipeline_version=self._pipeline_version,
            available_tools=[
                ToolDescriptor(d.name, d.description, d.input_schema) for d in descriptors
            ],
            parser_hypothesis_count=parser_hypothesis_count,
        )

        # ----- 3. Build the initial user message ----------------------------
        user_message = self._build_user_message(state, re_extract_hints)

        messages: list[Any] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ]

        # ----- 4. ReAct loop ------------------------------------------------
        llm = self._make_llm()
        llm_with_tools = llm.bind_tools(tools)

        final_payload: dict[str, Any] | None = None
        retry_count = 0
        ai_msg = None

        for iteration in range(1, self._max_iterations + 1):
            result.iterations_used = iteration
            try:
                ai_msg = await llm_with_tools.ainvoke(messages)
            except Exception as exc:
                raise AgentError(
                    f"Extractor LLM call failed at iteration {iteration}: {exc}",
                    retry_safe=True,
                    context={"agent_id": self.agent_id, "iteration": iteration,
                              "error": str(exc)},
                ) from exc
            messages.append(ai_msg)

            # Track tokens.
            ti, to = self._extract_usage(ai_msg)
            result.tokens_input += ti
            result.tokens_output += to

            tool_calls = list(getattr(ai_msg, "tool_calls", []) or [])
            content_text = self._content_to_text(ai_msg.content)
            self._record_trace(result, "ai", content_text, tool_calls)

            if tool_calls:
                result.tool_calls_made += len(tool_calls)
                tool_messages = await self._execute_tool_calls(tool_calls, tools_by_name, result)
                messages.extend(tool_messages)
                continue

            # No tool calls — the LLM should have emitted Final Answer JSON.
            content = content_text.strip()
            try:
                final_payload = self._parse_and_validate(content)
                break
            except (json.JSONDecodeError, SchemaValidationError, AgentError) as exc:
                retry_count += 1
                result.final_answer_retry_count = retry_count
                if retry_count > 1:
                    raise AgentError(
                        "Extractor failed to emit valid final JSON after 2 attempts",
                        retry_safe=False,
                        context={"agent_id": self.agent_id,
                                 "last_error": str(exc),
                                 "iterations_used": iteration},
                    ) from exc
                logger.warning(
                    "Extractor %s: final JSON invalid (%s) — injecting correction message",
                    self.agent_id, exc,
                )
                # Inject a correction message and let the LLM try once more.
                correction = (
                    "Your previous answer was not valid JSON for the "
                    f"`{self._schema_section}` schema section. Error: "
                    f"{exc}. Emit the JSON object exactly, no markdown fences, "
                    "no preamble, no fields outside the schema."
                )
                messages.append(HumanMessage(content=correction))
                continue
        else:
            raise AgentError(
                f"Extractor exceeded MAX_ITERATIONS={self._max_iterations} "
                f"without producing a Final Answer",
                retry_safe=False,
                context={"agent_id": self.agent_id, "team": self._team_name},
            )

        if final_payload is None:
            raise AgentError(
                "Extractor terminated without a final payload",
                retry_safe=False,
                context={"agent_id": self.agent_id},
            )

        # Extract llm_confidence_score from the payload if present.
        conf = final_payload.get("llm_confidence_score")
        if isinstance(conf, (int, float)):
            result.llm_confidence_score = float(conf)

        result.output = final_payload
        result.latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "Extractor %s: doc_id=%s done in %dms, %d iter(s), %d tool_call(s), conf=%s",
            self.agent_id, state.get("doc_id"), result.latency_ms,
            result.iterations_used, result.tool_calls_made, result.llm_confidence_score,
        )
        return result

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _build_user_message(
        self,
        state: PipelineState,
        re_extract_hints: list[dict[str, str]] | None,
    ) -> str:
        """Compose the initial human turn: per-page text + Block Profiler tags
        + (optional) Arbiter hints from a previous attempt."""
        doc_profile = state.get("doc_profile") or {}
        pages = doc_profile.get("pages") or []
        total_pages = doc_profile.get("total_pages") or len(pages)

        lines: list[str] = []
        lines.append(f"Source document: doc_id={state.get('doc_id')}, total_pages={total_pages}")

        if re_extract_hints:
            lines.append("")
            lines.append("**Hints from the previous attempt (Arbiter feedback):**")
            for h in re_extract_hints:
                lines.append(f"  - {h.get('field_name')}: {h.get('hint')}")

        lines.append("")
        lines.append("---")
        for p in pages:
            pnum = p.get("page_number") or 1
            text = p.get("text", "") or ""
            lines.append(f"## Page {pnum}")
            lines.append(text.strip() or "(empty)")
            lines.append("")

        return "\n".join(lines)

    async def _execute_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        tools_by_name: dict[str, Any],
        result: ExtractorResult,
    ) -> list[Any]:
        """Run a batch of tool calls concurrently. Returns ToolMessages."""
        from langchain_core.messages import ToolMessage

        async def _one(tc: dict[str, Any]) -> Any:
            name = tc.get("name") or ""
            args = tc.get("args") or {}
            tc_id = tc.get("id") or ""
            tool = tools_by_name.get(name)
            if tool is None:
                payload = {"error": f"Unknown tool {name!r}; available: {sorted(tools_by_name)}"}
                self._record_trace(result, "tool", json.dumps(payload), [], tool_name=name)
                return ToolMessage(content=json.dumps(payload), tool_call_id=tc_id)
            try:
                raw = tool.invoke(args)
            except Exception as exc:
                payload = {"error": f"{type(exc).__name__}: {exc}"}
                self._record_trace(result, "tool", json.dumps(payload), [], tool_name=name)
                return ToolMessage(content=json.dumps(payload), tool_call_id=tc_id)
            content = raw if isinstance(raw, str) else json.dumps(raw, default=str)
            self._record_trace(result, "tool", content, [], tool_name=name)
            return ToolMessage(content=content, tool_call_id=tc_id)

        return await asyncio.gather(*[_one(tc) for tc in tool_calls])

    def _parse_and_validate(self, content: str) -> dict[str, Any]:
        """Parse JSON from the LLM's final message and validate against the
        team's schema section."""
        if not content.strip():
            raise AgentError(
                "Extractor returned an empty final message",
                retry_safe=False,
                context={"agent_id": self.agent_id},
            )

        text = content.strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            m = _FINAL_JSON_RE.search(text)
            if not m:
                raise
            payload = json.loads(m.group(1) or m.group(2))

        # Pydantic validation against the team's section.
        self._schema_loader.validate(self._schema_section, payload)
        return payload

    @staticmethod
    def _content_to_text(content: Any) -> str:
        """Coerce langchain message content to plain text.

        langchain-google-genai 4.x returns `ai_msg.content` as either:
          - a plain str (text-only response)
          - a list of "parts" when the response mixes text + tool calls or
            structured output, where each part is either a str or a
            {"type": "text", "text": "..."} dict.
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict):
                    # The {"type": "text", "text": "..."} shape used by langchain.
                    if "text" in p and isinstance(p["text"], str):
                        parts.append(p["text"])
            return "".join(parts)
        return str(content)

    @staticmethod
    def _record_trace(
        result: ExtractorResult,
        role: str,
        content: Any,
        tool_calls: list[dict[str, Any]],
        *,
        tool_name: str | None = None,
    ) -> None:
        text = Extractor._content_to_text(content)
        entry: dict[str, Any] = {
            "role": role,
            "content": text[:2000],
        }
        if tool_calls:
            entry["tool_calls"] = [
                {"name": tc.get("name"), "args": tc.get("args")} for tc in tool_calls
            ]
        if tool_name:
            entry["tool_name"] = tool_name
        result.reasoning_trace.append(entry)
