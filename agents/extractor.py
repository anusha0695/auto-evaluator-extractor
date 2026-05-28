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

# Schema-scalar fields the model sometimes emits as a 1-element list (it conflates the
# singular item field with a plural umbrella list, e.g. `page_number` vs `page_numbers`).
# Set lives in config/section_layout.yaml:scalar_keys — adding a key is a YAML edit.
def _scalar_tic_keys() -> frozenset[str]:
    from agents.linker import _scalar_tic_keys as _src
    return _src()


def _coerce_scalar_tics(obj: Any, _keys: frozenset[str] | None = None) -> None:
    """In-place repair of common LLM type-tics before schema validation: a known scalar
    field emitted as a 1-element list → its element (`[1]` → `1`); an empty list → null.
    Recurses dicts/lists. Only touches keys declared in section_layout.yaml; real plural
    lists (e.g. `page_numbers`, `provenance`) are untouched. No-op when nothing matches."""
    keys = _keys if _keys is not None else _scalar_tic_keys()
    if not keys:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, list):
                obj[k] = v[0] if len(v) == 1 else (None if not v else v)
            else:
                _coerce_scalar_tics(v, keys)
    elif isinstance(obj, list):
        for it in obj:
            _coerce_scalar_tics(it, keys)


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
        forced_final_injected = False

        for iteration in range(1, self._max_iterations + 1):
            result.iterations_used = iteration
            # Force a Final Answer on the last TWO permitted iterations: drop the
            # tools and demand JSON. Without this, a tool-happy model (e.g. a
            # variant team calling hgnc_normalize/hgvs_validate every turn) can burn
            # the whole budget without ever finalizing → MAX_ITERATIONS error. A team
            # that converges normally finalizes well before this, so it's unaffected.
            force_final = iteration >= self._max_iterations - 1
            active_llm = llm if force_final else llm_with_tools
            if force_final and not forced_final_injected:
                forced_final_injected = True
                messages.append(HumanMessage(content=(
                    "You have reached the tool-use budget. STOP calling tools and emit "
                    f"the Final Answer: the JSON object for the `{self._schema_section}` "
                    "schema section, exactly — no markdown fences, no preamble, no commentary.")))
            try:
                ai_msg = await active_llm.ainvoke(messages)
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

            # When forcing the final answer the LLM is bound WITHOUT tools, so any
            # tool_calls it hallucinates can't be executed — ignore them and parse
            # the content as the Final Answer instead.
            if tool_calls and not force_final:
                result.tool_calls_made += len(tool_calls)
                tool_messages = await self._execute_tool_calls(tool_calls, tools_by_name, result)
                messages.extend(tool_messages)
                continue

            # No tool calls — the LLM should have emitted Final Answer JSON.
            content = content_text.strip()
            try:
                final_payload = await self._structured_or_parse(content, messages)
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

        # P3-M2 (Option B): a compact index of the blocks the Block Profiler
        # routed to THIS team's section (block_id / page / role / bbox / snippet),
        # so the Extractor sees the page's structure up front for accurate
        # binding + `occurrences`, without a docai_layout_lookup round-trip. Full
        # page text still follows; the layout/search tools remain for drill-down.
        block_index = self._section_block_index(doc_profile)
        if block_index:
            lines.append("")
            lines.append(f"## Blocks the profiler routed to `{self._schema_section}`")
            lines.append("Locate + ground your findings here; cite the `block_id` in each "
                         "`occurrences[]`. The full page text follows for anything not indexed.")
            lines.append("| block_id | page | role | bbox | text (first 80 chars) |")
            lines.append("|---|---|---|---|---|")
            for r in block_index:
                lines.append(f"| {r['block_id']} | {r['page']} | {r['role']} | {r['bbox']} | {r['snippet']} |")

        lines.append("")
        lines.append("---")
        for p in pages:
            pnum = p.get("page_number") or 1
            text = p.get("text", "") or ""
            lines.append(f"## Page {pnum}")
            lines.append(text.strip() or "(empty)")
            lines.append("")

        return "\n".join(lines)

    def _section_block_index(self, doc_profile: dict[str, Any]) -> list[dict[str, Any]]:
        """P3-M2 (Option B): compact index of blocks the Block Profiler routed to
        THIS team's section. Joins `block_profiles` (role + hints) with `blocks`
        (bbox + text) by block_id. Returns [] when profiles/blocks are absent
        (e.g. older runs) so the prompt degrades gracefully to page-text only."""
        profiles = doc_profile.get("block_profiles") or []
        blocks = doc_profile.get("blocks") or []
        by_id = {b.get("block_id"): b for b in blocks if isinstance(b, dict)}
        out: list[dict[str, Any]] = []
        for bp in profiles:
            if not isinstance(bp, dict):
                continue
            if self._schema_section not in (bp.get("target_umbrella_hints") or []):
                continue
            bid = bp.get("block_id")
            blk = by_id.get(bid) or {}
            text = " ".join((blk.get("text") or "").split())
            snippet = (text[:80] + "…") if len(text) > 80 else text
            bbox = blk.get("bbox")
            bbox_s = "[" + ",".join(f"{c:.2f}" for c in bbox) + "]" if isinstance(bbox, list) and bbox else ""
            out.append({
                "block_id": bid,
                "page": bp.get("page_number") or blk.get("page_number") or "",
                "role": bp.get("text_role") or "",
                "bbox": bbox_s,
                "snippet": snippet.replace("|", "/"),   # don't break the md table
            })
            if len(out) >= 60:   # bound the index
                break
        return out

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

    async def _structured_or_parse(self, content: str, messages: Any) -> dict[str, Any]:
        """Final-answer parse. DEFAULT (flag off) = free-text `_parse_and_validate`
        (identical to before). When EXTRACTOR_STRUCTURED is on, first try a Gemini
        `json_schema` structured finalizer (Gemini enforces the section shape so the
        model can't wrap/mis-shape it); on ANY error fall back to the free-text path,
        so this can never degrade the default. NOT yet validated against the deep
        biomarker schema — gate via env + A/B before flipping on by default."""
        import os
        if os.environ.get("EXTRACTOR_STRUCTURED", "0").lower() in ("1", "true", "yes", "on"):
            try:
                section = self._schema_loader.get_section(self._schema_section)
                model = getattr(section, "pydantic_model", None)
                if model is not None:
                    structured = self._make_llm().with_structured_output(model, method="json_schema")
                    result = await structured.ainvoke(messages)
                    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
                    self._schema_loader.validate(self._schema_section, payload)
                    logger.info("Extractor %s: json_schema finalizer succeeded", self.agent_id)
                    return payload
            except Exception:  # noqa: BLE001 — never let the finalizer regress the default
                logger.warning("Extractor %s: json_schema finalizer failed → free-text fallback",
                               self.agent_id, exc_info=True)
        return self._parse_and_validate(content)

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
                self._dump_failure(content, payload=None, error="final answer is not JSON")
                raise
            payload = json.loads(m.group(1) or m.group(2))

        # Unwrap section-wrapped payloads: the model sometimes emits
        # `{"Genomic_Variant_umbrella": {...real fields...}}` instead of
        # `{...real fields...}` at top level (re-stating the section name in the
        # response is a common LLM tic, especially after a correction message).
        # The wrap is unambiguous: there's exactly one top-level key and it
        # equals the team's section name. Unwrap once before downstream repair
        # + validation so we don't burn the second retry on the same error.
        if (isinstance(payload, dict)
                and len(payload) == 1
                and self._schema_section in payload
                and isinstance(payload[self._schema_section], dict)):
            logger.info(
                "Extractor[%s]: unwrapped section-wrapped final payload "
                "(top-level key == section name)", self._schema_section)
            payload = payload[self._schema_section]

        # Repair common LLM type-tics before validation (e.g. a scalar `page_number`
        # emitted as a 1-element list because the model conflates it with the plural
        # `page_numbers`). Deterministic + scoped — see _coerce_scalar_tics.
        _coerce_scalar_tics(payload)

        # Pydantic validation against the team's section.
        try:
            self._schema_loader.validate(self._schema_section, payload)
        except Exception as exc:  # noqa: BLE001 — dump the offending payload, then re-raise
            self._dump_failure(content, payload=payload, error=str(exc))
            raise
        return payload

    def _dump_failure(self, content: str, *, payload: Any, error: str) -> None:
        """DEBUG: when the extractor's final answer won't parse/validate, write the FULL
        raw content to a local file + log a summary (parsed top-level keys, whether the
        model WRAPPED the object under the section name, the exact error). Local-only;
        the raw content may contain PHI — never pushed. Remove once the shape issue is
        understood."""
        import os
        import time
        keys = sorted(payload.keys()) if isinstance(payload, dict) else f"<{type(payload).__name__}>"
        wrapped = isinstance(payload, dict) and self._schema_section in payload
        try:
            dbg_dir = os.path.join("local_runs", "_extractor_debug")
            os.makedirs(dbg_dir, exist_ok=True)
            path = os.path.join(dbg_dir, f"{self._schema_section}_{int(time.time())}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"agent={self.agent_id} section={self._schema_section}\n")
                fh.write(f"parsed_top_level_keys={keys}\nwrapped_under_section_name={wrapped}\n")
                fh.write(f"error={error}\n\n=== RAW FINAL CONTENT (verbatim) ===\n{content}\n")
            logger.error("EXTRACTOR-DEBUG[%s]: parse/validate FAILED → wrote %s | "
                         "top_keys=%s wrapped_under_section=%s | error=%s",
                         self._schema_section, path, keys, wrapped, error[:300])
        except Exception:  # noqa: BLE001 — debug dump must never mask the real error
            logger.error("EXTRACTOR-DEBUG[%s]: parse/validate FAILED (dump-write failed) | "
                         "top_keys=%s wrapped_under_section=%s | error=%s",
                         self._schema_section, keys, wrapped, error[:300])

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
