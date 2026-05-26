"""
Planner — deterministic team activation (Phase 2, M4).

Decides which Specialist Teams graph_linear should run for a document. No LLM: the
signal already exists in preprocessing output.

A team is ACTIVE iff its `schema_section` is signalled by either:
  - a Block Profiler hint  — any block's `target_umbrella_hints` contains it, OR
  - a parser-hypothesis candidate — any candidate's `target_umbrella` equals it.

`report_metadata` is always active (every report has administrative facts).
`always_active` covers that; the `safety_override` makes activation err toward
running (recall at the planning layer) — a team is skipped ONLY when there is no
signal of any confidence for its section.

Cost/latency are proportional to document content: a single-gene PCR report
skips the molecular-biomarker and clinical-info teams; a rich NGS panel runs more.
The planner logs the skip rationale so a missed-signal recall risk is auditable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.observability import trace as otel_trace
from core.state import PipelineState

logger = logging.getLogger(__name__)

# Sections that always run regardless of signal.
_ALWAYS_ACTIVE_SECTIONS = frozenset({"report_metadata"})


@dataclass
class PlanResult:
    active_team_keys: list[str] = field(default_factory=list)
    skipped_team_keys: list[str] = field(default_factory=list)
    rationale: dict[str, str] = field(default_factory=dict)   # team_key → why
    signalled_sections: list[str] = field(default_factory=list)


class Planner:
    """Deterministic team router. Stateless; one instance reused across docs."""

    def __init__(self, *, always_active_sections: frozenset[str] = _ALWAYS_ACTIVE_SECTIONS) -> None:
        self._always = always_active_sections

    @otel_trace("agents.planner.plan")
    def plan(
        self,
        state: PipelineState,
        *,
        teams_cfg: dict[str, Any],
        team_keys: list[str],
    ) -> PlanResult:
        """Return the activation plan for `team_keys` (the teams graph_linear manages).

        Args:
            state: PipelineState after preprocessing (needs doc_profile.block_profiles
                   and parser_hypothesis.candidates).
            teams_cfg: parsed teams.yaml (for team_key → schema_section).
            team_keys: which teams to consider.
        """
        signalled = self._signalled_sections(state)
        out = PlanResult(signalled_sections=sorted(signalled))

        for key in team_keys:
            section = teams_cfg["teams"][key]["schema_section"]
            if section in self._always:
                out.active_team_keys.append(key)
                out.rationale[key] = f"always-active section '{section}'"
            elif section in signalled:
                out.active_team_keys.append(key)
                out.rationale[key] = f"section '{section}' signalled by block hint / parser hypothesis"
            else:
                out.skipped_team_keys.append(key)
                out.rationale[key] = f"no block hint or candidate targets '{section}' — skipped"

        logger.info(
            "Planner: doc_id=%s active=%s skipped=%s",
            state.get("doc_id"), out.active_team_keys, out.skipped_team_keys,
        )
        return out

    # -----------------------------------------------------------------------

    @staticmethod
    def _signalled_sections(state: PipelineState) -> set[str]:
        """Union of every umbrella section signalled by block hints OR parser
        hypothesis candidates. Excludes the 'none' sentinel."""
        signalled: set[str] = set()

        doc_profile = state.get("doc_profile") or {}
        for bp in (doc_profile.get("block_profiles") or []):
            for hint in (bp.get("target_umbrella_hints") or []):
                if hint and hint != "none":
                    signalled.add(hint)

        ph = state.get("parser_hypothesis") or {}
        for cand in (ph.get("candidates") or []):
            tgt = cand.get("target_umbrella") if isinstance(cand, dict) else None
            if tgt and tgt != "none":
                signalled.add(tgt)

        return signalled
