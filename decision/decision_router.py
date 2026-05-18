"""
DecisionRouter — picks the final verdict for a document.

Inputs (read from PipelineState):
  - `_team_verdict` / `_team_verdict_reason` set by metadata_team_node.
  - `verifier_scorecards` (a list; Phase 1 has just `schema_validator`).
  - `team_outputs.metadata_team.llm_confidence_score` for the
    confidence-vs-threshold check.

Resolution policy (top match wins):

    1. Team verdict == "sme_flag"                  → sme_flag
    2. Any verifier scorecard.passed == False      → sme_flag
    3. `llm_confidence_score` < threshold          → sme_flag
    4. Otherwise                                    → auto_accept

The threshold defaults to `AUTO_ACCEPT_CONFIDENCE_THRESHOLD` env var
(default 0.85, matching `config/teams.yaml`'s
`auto_accept_confidence_threshold`).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from core.observability import trace as otel_trace
from core.state import PipelineState

logger = logging.getLogger(__name__)


Verdict = Literal["auto_accept", "sme_flag"]


class DecisionRouter:
    """Stateless verdict picker. One instance per pipeline boot is fine."""

    def __init__(self, *, auto_accept_confidence_threshold: float | None = None) -> None:
        self._threshold = float(
            auto_accept_confidence_threshold
            if auto_accept_confidence_threshold is not None
            else os.environ.get("AUTO_ACCEPT_CONFIDENCE_THRESHOLD", "0.85")
        )

    @otel_trace("decision.decision_router.decide")
    def decide(self, state: PipelineState) -> tuple[Verdict, str]:
        """Pick the verdict + reason. Pure function of state."""
        # 1. Team-level SME flag propagates.
        if state.get("_team_verdict") == "sme_flag":
            reason = (
                f"MetadataTeam emitted sme_flag: "
                f"{state.get('_team_verdict_reason') or '(no reason)'}"
            )
            return "sme_flag", reason

        # 2. Verifier scorecards — any failure → sme_flag.
        scorecards = list(state.get("verifier_scorecards") or [])
        failed = [s for s in scorecards if not s.get("passed", True)]
        if failed:
            first = failed[0]
            reason = (
                f"Verifier {first.get('verifier_name','?')!r} failed with "
                f"{len(first.get('field_errors') or [])} error(s): "
                f"{first.get('notes', '')[:300]}"
            )
            return "sme_flag", reason

        # 3. Confidence threshold.
        team_outputs = state.get("team_outputs") or {}
        metadata_output = team_outputs.get("metadata_team") or {}
        conf = metadata_output.get("llm_confidence_score")
        if isinstance(conf, (int, float)) and conf < self._threshold:
            reason = (
                f"llm_confidence_score={float(conf):.2f} is below "
                f"auto-accept threshold {self._threshold:.2f}"
            )
            return "sme_flag", reason

        # 4. Default: auto-accept.
        reason = (
            "All verifiers passed and llm_confidence_score "
            f"({conf if conf is not None else '<absent>'}) ≥ {self._threshold:.2f}"
        )
        return "auto_accept", reason


# ---------------------------------------------------------------------------
# LangGraph node factory
# ---------------------------------------------------------------------------


def make_decision_router_node(*, router: DecisionRouter | None = None):
    """Return an async LangGraph node bound to the router."""
    router = router or DecisionRouter()

    @otel_trace("decision.decision_router.node")
    async def decision_router_node(state: PipelineState) -> dict[str, Any]:
        verdict, reason = router.decide(state)
        logger.info(
            "decision_router: doc_id=%s verdict=%s — %s",
            state.get("doc_id"), verdict, reason[:120],
        )
        return {"verdict": verdict, "verdict_reason": reason}

    return decision_router_node
