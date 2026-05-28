"""
Per-agent trace assembly (P3-M8b) — the "how this field got extracted" record.

`build_team_trace(result, team_key)` turns a `SectionTeamResult` (the live
Extractor / CoverageAuditor / Arbiter / re-extract result objects a team produced)
into an ordered list of `AgentTraceRecord` dicts: which agent ran, what it acted on
(input_summary), and what it decided (output_summary + verdict + confidence). The
SME UI (M8c/M8d) merges these with `repair_log` + `vmaw_log` into a single
field-centric timeline, in either a technical or a plain-language rendering.

PHI discipline: summaries are STRUCTURAL — counts, verdicts, hint counts, short
model-reasoning snippets — never a dump of the raw rendered prompt (which contains
page text / PHI). The full extracted values live in the extraction artifact the UI
already shows; the trace explains the DECISION FLOW, not the clinical content.
Everything here is pure / offline — the gate drives it with stub result objects.
"""

from __future__ import annotations

from typing import Any


def _conf(r: Any) -> float | None:
    c = getattr(r, "llm_confidence_score", None)
    return float(c) if isinstance(c, (int, float)) else None


def _n_fields(output: Any) -> int:
    """Count populated top-level values in a section output (no values leaked)."""
    if not isinstance(output, dict):
        return 0
    n = 0
    for v in output.values():
        if v in (None, "", [], {}):
            continue
        n += len(v) if isinstance(v, list) else 1
    return n


def _rec(step: int, agent: str, team: str, section: str, *,
         input_summary: str, output_summary: str, verdict: str,
         plain: str = "",
         phase: str = "extraction",
         confidence: float | None = None, reasoning: str = "",
         field_reasons: dict[str, str] | None = None, r: Any = None) -> dict[str, Any]:
    # `plain` is the canonical one-sentence summary the UI renders verbatim. Set it
    # HERE at record time — the UI must not regex against agent names to invent it.
    # `phase` is also set HERE (default "extraction" — every team-trace record is an
    # extraction-phase event); special phases (planning, linking, …) come from the
    # nodes in pipeline/graph_*.py via core.trace_recorder.record.
    return {
        "step": step, "phase": phase, "agent": agent, "team": team, "section": section,
        "plain": (plain or "")[:300],
        "input_summary": input_summary, "output_summary": output_summary,
        "verdict": verdict, "confidence": confidence,
        "reasoning": (reasoning or "").strip()[:1500],  # section-level "why" (PHI: local-only)
        "field_reasons": field_reasons or {},           # {field_name: per-field reason}
        "latency_ms": int(getattr(r, "latency_ms", 0) or 0) if r is not None else 0,
        "tool_calls": int(getattr(r, "tool_calls_made", 0) or 0) if r is not None else 0,
    }


def _extractor_reasoning(r: Any) -> str:
    """Best-effort: the extractor's last natural-language thought from its ReAct
    trace. Prefer the content of a `<reasoning>...</reasoning>` block (the base
    prompt requires the model to emit one before the final JSON). Fall back to
    the most recent natural-language assistant message. Skip tool-call turns
    AND raw-JSON / code-fence dumps so we never surface the output blob."""
    for entry in reversed(list(getattr(r, "reasoning_trace", None) or [])):
        if not isinstance(entry, dict):
            continue
        if (entry.get("role") or "").lower() not in ("assistant", "ai", "model"):
            continue
        c = str(entry.get("content") or "").strip()
        if not c:
            continue
        # Preferred: pull the reasoning block out of the final-answer message —
        # that's where the model is now told to write its thought process.
        block = _extract_reasoning_block(c)
        if block:
            return block
        # Skip raw JSON dumps and tool-call turns for the fallback path.
        if entry.get("tool_calls"):
            continue
        if c.lstrip()[:1] in ("{", "[", '"') or c.lstrip().startswith("```"):
            continue
        return c
    return ""


def _kv(obj: Any, key: str, default: str = "") -> Any:
    """field_name/why off a pydantic MissedField OR a plain dict."""
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def build_team_trace(result: Any, *, team_key: str) -> list[dict[str, Any]]:
    """Ordered agent-trace records for one team's run. Duck-typed on
    SectionTeamResult so it's testable with stubs."""
    team = team_key
    section = getattr(result, "schema_section", "") or ""
    out: list[dict[str, Any]] = []
    step = 0

    ex = getattr(result, "extractor_result", None)
    if ex is not None:
        out.append(_rec(step, "Extractor", team, section,
                        plain="Our system read this section of the report and pulled out the values it found.",
                        input_summary="read routed blocks + full page text; considered parser-hypothesis candidates",
                        output_summary=f"produced {_n_fields(getattr(ex, 'output', None))} populated field(s)",
                        verdict="extracted", confidence=_conf(ex),
                        reasoning=_extractor_reasoning(ex), r=ex))
        step += 1
        # Surface the FULL ReAct chain — one record per Thought / Tool-call so the
        # field timeline can show the model's intermediate reasoning, the exact tool
        # calls it made, and each tool's return. Filtered out of the summary list when
        # the UI doesn't want fine-grained steps.
        step = _expand_reasoning_trace(out, getattr(ex, "reasoning_trace", []) or [],
                                       team=team, section=section, attempt="initial", step=step)

    au = getattr(result, "auditor_result", None)
    if au is not None:
        ok = bool(getattr(au, "coverage_ok", True))
        gap = bool(getattr(au, "gap_signal", False))
        misses = len(getattr(au, "parser_hypothesis_misses", []) or [])
        # per-field reasons: which fields the auditor flagged + WHY.
        au_fr: dict[str, str] = {}
        for mf in (getattr(au, "missed_fields", []) or []):
            fn, why = _kv(mf, "field_name"), _kv(mf, "why_extractor_should_have_caught_it")
            if fn and why:
                au_fr[str(fn)] = "missed — " + str(why)
        for sf in (getattr(au, "spurious_fields", []) or []):
            fn, ev = _kv(sf, "field_name"), _kv(sf, "evidence_against")
            if fn and ev:
                au_fr[str(fn)] = "looks spurious — " + str(ev)
        au_plain = ("A second check compared what was pulled out against what the report seemed to contain"
                    + (" and everything lined up." if (ok and not gap)
                       else " and noticed something might be missing."))
        out.append(_rec(step, "CoverageAuditor", team, section,
                        plain=au_plain,
                        input_summary="reviewed the extractor's output against expected coverage",
                        output_summary=f"coverage_ok={ok}, gap_signal={gap}, {misses} hypothesis miss(es)",
                        verdict="coverage_ok" if (ok and not gap) else "gap_flagged",
                        reasoning=str(getattr(au, "auditor_notes", "") or ""),
                        field_reasons=au_fr, r=au))
        step += 1

    ar = getattr(result, "arbiter_result", None)
    if ar is not None:
        policy = getattr(ar, "policy", "")
        hint_list = getattr(ar, "re_extract_hints", []) or []
        ar_fr = {str(_kv(h, "field_name")): str(_kv(h, "hint"))
                 for h in hint_list if _kv(h, "field_name") and _kv(h, "hint")}
        out.append(_rec(step, "Arbiter", team, section,
                        plain="Because the two steps disagreed, a referee step decided how to proceed.",
                        input_summary="resolved the extractor↔auditor disagreement",
                        output_summary=f"policy={policy}; {len(hint_list)} hint(s)",
                        verdict=str(policy),
                        reasoning=str(getattr(ar, "reasoning", "") or ""),
                        field_reasons=ar_fr, r=ar))
        step += 1

    rx = getattr(result, "extractor_result_retry", None)
    if rx is not None:
        nhints = len(getattr(ar, "re_extract_hints", []) or []) if ar is not None else 0
        out.append(_rec(step, "Extractor (re-extract)", team, section,
                        plain="Our system took another careful pass at this section after a check raised a concern.",
                        input_summary=f"re-ran with {nhints} focused hint(s)",
                        output_summary=f"re-extracted {_n_fields(getattr(rx, 'output', None))} populated field(s)",
                        verdict="re_extracted", confidence=_conf(rx),
                        reasoning=_extractor_reasoning(rx), r=rx))
        step += 1
        step = _expand_reasoning_trace(out, getattr(rx, "reasoning_trace", []) or [],
                                       team=team, section=section, attempt="re-extract", step=step)

    return out


def _is_json_only(text: str) -> bool:
    """True iff `text` is a JSON/code-fence dump with no natural-language wrapper —
    used to distinguish a real Thought (the model reasoning out loud) from the model
    emitting its final answer directly as JSON. Same heuristic as the UI cleaner so
    record-time labels and render-time labels agree."""
    t = (text or "").strip()
    if not t:
        return False
    if t.startswith("```"):
        return True
    if t[:1] in ("{", "["):
        return any(c in t for c in (":", ","))
    return False


import re as _re
_REASONING_RE = _re.compile(r"<reasoning>(.*?)</reasoning>", _re.DOTALL | _re.IGNORECASE)


def _extract_reasoning_block(text: str) -> str:
    """Return the inner text of the first <reasoning>…</reasoning> block. The
    base Extractor prompt requires this block before the final JSON — pulling
    it out lets the trace surface the model's actual reasoning instead of the
    raw '<reasoning>…</reasoning>{json}' string."""
    m = _REASONING_RE.search(text or "")
    return (m.group(1).strip() if m else "")


def _expand_reasoning_trace(
    out: list[dict[str, Any]], trace: list[dict[str, Any]], *,
    team: str, section: str, attempt: str, step: int,
) -> int:
    """Append one record per Thought / Tool-call / Tool-result to `out`. Returns the
    next step counter. PHI-safe (content already truncated to 2000 chars by the
    Extractor's own recorder; we truncate again to 500 for the summary line).

    Three message shapes we distinguish:
      • assistant text + tool_calls → a real Thought (the model reasoned, then
        chose tools). Surface the thought text verbatim.
      • assistant text only, JSON-only content (no tool_calls) → the model
        emitted its final answer directly with no intermediate reasoning. Record
        as `Extractor · final answer` so the SME isn't told the model "paused to
        think" when it didn't.
      • assistant natural-language text only (no JSON, no tool_calls) → a
        terminal thought just before final answer. Record as Thought with the
        actual content as the plain line."""
    thought_n = 0
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        role = (entry.get("role") or "").lower()
        text = str(entry.get("content") or "")
        tcs = entry.get("tool_calls") or []
        tool_name = entry.get("tool_name")
        if role in ("assistant", "ai", "model"):
            text_s = text.strip()
            json_only = _is_json_only(text_s)
            # Reasoning block extracted from <reasoning>...</reasoning> tags, if
            # present (the base prompt requires the model to emit one before its
            # final JSON). This is the REAL thought — surface it directly.
            reasoning_block = _extract_reasoning_block(text_s)
            # First-line preview of the model's content — used only as a
            # fallback when there's no reasoning block.
            short = text_s.split("\n", 1)[0][:500] if text_s else ""
            if json_only and not tcs:
                # Pure JSON, no <reasoning> block, no tool calls — model went
                # straight to output without externalising anything. The base
                # prompt now asks for a reasoning block; if we see this shape,
                # the model ignored that ask. Surface that fact honestly.
                out.append(_rec(step, "Extractor · final answer", team, section,
                                plain=("The model produced its structured answer "
                                       "in one step without an externalised "
                                       "reasoning block — its reasoning happened "
                                       "internally."),
                                input_summary=f"attempt={attempt}",
                                output_summary="produced final JSON output",
                                verdict="final_answer",
                                reasoning="", r=None))
                step += 1
                continue
            # Real thought — either a <reasoning> block, or natural-language
            # text (with or without tool calls).
            thought_n += 1
            if reasoning_block:
                # The model emitted the required reasoning block — quote it
                # verbatim so the SME sees the actual thought process.
                first_line = reasoning_block.split("\n", 1)[0][:500]
                t_plain = f"Thought #{thought_n} — {first_line}"
                t_reasoning = reasoning_block[:1500]
            elif text_s and not json_only:
                # Natural-language text with no <reasoning> tags — still a real
                # thought, surface its first line.
                t_plain = f"Thought #{thought_n} — “{short}”" if short else \
                          f"Thought #{thought_n} (no text)"
                t_reasoning = text[:1500]
            else:
                # Tool-call-only turn (no text content, just tool calls).
                t_plain = f"The model chose to call {len(tcs)} tool(s) — step {thought_n}."
                t_reasoning = text[:1500]
            out.append(_rec(step, f"Extractor · thought #{thought_n}", team, section,
                            plain=t_plain,
                            input_summary=f"attempt={attempt}",
                            output_summary=(short or f"called {len(tcs)} tool(s)"),
                            verdict="thinking",
                            reasoning=t_reasoning, r=None))
            step += 1
            # one record per tool call requested in this thought
            for tc in tcs:
                tname = str(tc.get("name") or "tool")
                args = tc.get("args")
                out.append(_rec(step, f"Extractor · tool · {tname}", team, section,
                                plain=f"The model asked the {tname} tool for help and waited for the answer.",
                                input_summary=f"args={str(args)[:200]}",
                                output_summary=f"{tname} called",
                                verdict="tool_called", r=None))
                step += 1
        elif role == "tool":
            tname = str(tool_name or "tool")
            short = text.strip().split("\n", 1)[0][:300]
            out.append(_rec(step, f"Extractor · {tname} → result", team, section,
                            plain=f"The {tname} tool returned its answer to the model.",
                            input_summary=f"{tname} returned",
                            output_summary=short[:500] or "(empty)",
                            verdict="tool_result",
                            reasoning=text[:1500], r=None))
            step += 1
    return step


def aggregate_team_traces(results_by_team: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every active team's trace into one list for the agent_trace channel."""
    out: list[dict[str, Any]] = []
    for team_key, result in (results_by_team or {}).items():
        out.extend(build_team_trace(result, team_key=team_key))
    return out
