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
         confidence: float | None = None, reasoning: str = "",
         field_reasons: dict[str, str] | None = None, r: Any = None) -> dict[str, Any]:
    return {
        "step": step, "agent": agent, "team": team, "section": section,
        "input_summary": input_summary, "output_summary": output_summary,
        "verdict": verdict, "confidence": confidence,
        "reasoning": (reasoning or "").strip()[:1500],  # section-level "why" (PHI: local-only)
        "field_reasons": field_reasons or {},           # {field_name: per-field reason}
        "latency_ms": int(getattr(r, "latency_ms", 0) or 0) if r is not None else 0,
        "tool_calls": int(getattr(r, "tool_calls_made", 0) or 0) if r is not None else 0,
    }


def _extractor_reasoning(r: Any) -> str:
    """Best-effort: the extractor's last natural-language thought from its ReAct
    trace. Skip tool-call turns AND the final answer (raw JSON, code fences, or a
    quoted/object dump) so we never surface the output blob as 'reasoning'."""
    for entry in reversed(list(getattr(r, "reasoning_trace", None) or [])):
        if not isinstance(entry, dict) or entry.get("tool_calls"):
            continue
        if (entry.get("role") or "").lower() not in ("assistant", "ai", "model"):
            continue
        c = str(entry.get("content") or "").strip()
        if not c or c.lstrip()[:1] in ("{", "[", '"') or c.lstrip().startswith("```"):
            continue                                     # that's the output dump, not a thought
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
                        input_summary="read routed blocks + full page text; considered parser-hypothesis candidates",
                        output_summary=f"produced {_n_fields(getattr(ex, 'output', None))} populated field(s)",
                        verdict="extracted", confidence=_conf(ex),
                        reasoning=_extractor_reasoning(ex), r=ex))
        step += 1

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
        out.append(_rec(step, "CoverageAuditor", team, section,
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
                        input_summary=f"re-ran with {nhints} focused hint(s)",
                        output_summary=f"re-extracted {_n_fields(getattr(rx, 'output', None))} populated field(s)",
                        verdict="re_extracted", confidence=_conf(rx),
                        reasoning=_extractor_reasoning(rx), r=rx))
        step += 1

    return out


def aggregate_team_traces(results_by_team: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every active team's trace into one list for the agent_trace channel."""
    out: list[dict[str, Any]] = []
    for team_key, result in (results_by_team or {}).items():
        out.extend(build_team_trace(result, team_key=team_key))
    return out
