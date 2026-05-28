"""
Agent-trace recorder — the canonical "every agent that ran on this doc" log.

Design: each agent node returns a state delta that *appends* one or more records to
`state["agent_trace"]`. LangGraph merges deltas, so by the end of the run agent_trace
holds an ordered list of EVERY invocation across: preprocess, planning, extraction,
linking, supersession, dedup, verification, triage, repair, vmaw, decision. Persistence
writes that list verbatim to `agent_trace.json` — one file = full trip.

UI consumption: `ui/phase1/field_trace.assemble_field_trace` filters by section/ref
over this unified list, instead of merging six separate channels (scorecards, links,
repair_log, vmaw_log, binding_items) with different shapes and visibility rules.

PHI discipline: summaries are STRUCTURAL — counts, verdicts, ref strings, short
model-reasoning snippets — never raw page text. The clinical values live in the
extraction artifact; the trace explains the DECISION FLOW.
"""

from __future__ import annotations

from typing import Any, Iterable

# Canonical phase order — the field-timeline renders in this order.
PHASES = (
    "preprocess",
    "planning",
    "extraction",
    "linking",
    "supersession",
    "dedup",
    "verification",
    "triage",
    "repair",
    "vmaw",
    "decision",
)


def record(
    *,
    phase: str,
    agent: str,
    plain: str = "",                          # one-sentence plain-language summary
    section: str | None = None,
    refs: Iterable[str] | None = None,
    input_summary: str = "",
    output_summary: str = "",
    verdict: str = "",
    reasoning: str = "",
    field_reasons: dict[str, str] | None = None,
    confidence: float | None = None,
    latency_ms: int | float | None = 0,
    tool_calls: int | None = 0,
    team: str = "",
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one trace record. PHI-safe by construction: all string fields are
    truncated; the caller is responsible for not passing raw page text."""
    if phase not in PHASES:
        # Don't reject — log under a wildcard phase so an unknown phase never breaks
        # the run. Add it to PHASES if it's a real new phase.
        phase = phase or "extension"
    rec: dict[str, Any] = {
        "step": 0,                              # set by `extend_trace` (auto-increment)
        "phase": phase,
        "agent": agent,
        "plain": (plain or "")[:300],            # rendered as-is by the UI; no regex needed
        "team": team or "",
        "section": section,
        "refs": [str(r) for r in (refs or [])],  # refs the agent acted on (for field-filter)
        "input_summary": (input_summary or "")[:300],
        "output_summary": (output_summary or "")[:500],
        "verdict": (verdict or "")[:80],
        "reasoning": (reasoning or "").strip()[:1500],
        "field_reasons": {str(k): str(v)[:500] for k, v in (field_reasons or {}).items()},
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
        "latency_ms": int(latency_ms or 0),
        "tool_calls": int(tool_calls or 0),
    }
    if extras:
        rec["extras"] = dict(extras)
    return rec


def extend_trace(existing: list[dict[str, Any]] | None, *new_records: dict[str, Any]) -> list[dict[str, Any]]:
    """Append `new_records` to `existing` with auto-incrementing `step`. Returns a
    fresh list (state semantics: replace the agent_trace channel with this list).
    Pass directly as `{"agent_trace": extend_trace(state.get("agent_trace"), rec, …)}`."""
    base = list(existing or [])
    next_step = (max((int(r.get("step", 0)) for r in base), default=-1) + 1) if base else 0
    for i, r in enumerate(new_records):
        if r is None:
            continue
        r["step"] = next_step + i
        base.append(r)
    return base


def filter_by_ref(
    trace: list[dict[str, Any]],
    *,
    ref: str | None = None,
    section: str | None = None,
) -> list[dict[str, Any]]:
    """Pure filter over the unified trace: keep records that touch the focused ref
    (any ref in `refs` shares an array-token like `Genomic_Variants[0]` with `ref`)
    OR whose `section` matches. Used by the field-timeline UI."""
    if not (ref or section):
        return list(trace or [])
    ref_toks = _ref_tokens(ref) if ref else set()
    out: list[dict[str, Any]] = []
    for r in (trace or []):
        if section and r.get("section") == section:
            out.append(r); continue
        if ref_toks:
            rrefs = r.get("refs") or []
            if any(_ref_tokens(rr) & ref_toks for rr in rrefs):
                out.append(r); continue
    return out


def _ref_tokens(ref: str | None) -> set[str]:
    """Token-set used by filter_by_ref to match cross-section refs by shared
    array-anchor (e.g. `other_molecular_biomarkers[0]`)."""
    import re
    if not ref:
        return set()
    toks = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*\[\d+\]", str(ref)):
        toks.add(m.group(0))
    if not toks:
        # fall back to last dotted segment (a leaf like `result`)
        leaf = str(ref).split(".")[-1].split("[")[0]
        if leaf:
            toks.add(leaf)
    return toks
