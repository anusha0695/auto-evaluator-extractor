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
from dataclasses import dataclass, field
from typing import Any, Literal

from core.observability import trace as otel_trace
from core.state import PipelineState

logger = logging.getLogger(__name__)


Verdict = Literal["auto_accept", "sme_flag"]
VerdictV2 = Literal["auto_accept", "fixable", "sme_flag"]
# P3-M4: partial accept — the doc can be partly committed, partly escalated.
VerdictV3 = Literal["auto_accept", "partial_accept", "sme_flag"]


@dataclass
class RouterDecisionV3:
    """Result of the P3-M4 partial-accept router. Per-section accept/flag + the
    record-level escalation items SME (or VMAW in M7) should review."""
    verdict: VerdictV3
    reason: str
    accepted_sections: list[str] = field(default_factory=list)
    flagged_sections: list[dict[str, Any]] = field(default_factory=list)   # {section, reasons}
    escalation_items: list[dict[str, Any]] = field(default_factory=list)   # {section, ref, kind, detail}

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "reason": self.reason,
            "accepted_sections": self.accepted_sections,
            "flagged_sections": self.flagged_sections,
            "escalation_items": self.escalation_items,
        }


# team_key → schema section (1:1). Used to report per-SECTION accept/flag.
_TEAM_SECTION = {
    "metadata_team": "report_metadata",
    "molecular_biomarker_team": "other_molecular_biomarker_umbrella",
    "tested_biomarker_team": "tested_biomarker_umbrella",
    "clinical_info_team": "clinical_information",
    "specimen_findings_team": "significant_findings",
}


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


    # -----------------------------------------------------------------------
    # Phase 2 — aggregate across all active teams + the full verifier suite
    # -----------------------------------------------------------------------

    @otel_trace("decision.decision_router.decide_v2")
    def decide_v2(self, state: PipelineState) -> tuple[VerdictV2, str]:
        """Aggregate verdict for graph_linear. Reads:
          - state["team_results"]: {team_key: {verdict, llm_confidence_score, needs_review_count}}
          - state["verifier_scorecards"]: all deterministic verifiers
          - state["binding_verifier"]: {"refuted": int, "uncertain": int}

        Policy (top match wins; ground truth never auto-ships an unverified claim):
          1. any team sme_flag                         → sme_flag
          2. any verifier scorecard failed             → sme_flag
          3. binding-verifier refuted > 0              → fixable (→ Arbiter re-extract/escalate)
          4. binding-verifier uncertain > 0            → sme_flag
          5. any team needs_review_count > 0           → sme_flag
          6. min team confidence < threshold           → sme_flag
          7. otherwise                                 → auto_accept
        """
        team_results = state.get("team_results") or {}

        sme_teams = [k for k, r in team_results.items() if (r or {}).get("verdict") == "sme_flag"]
        if sme_teams:
            return "sme_flag", f"Team(s) emitted sme_flag: {sme_teams}"

        scorecards = list(state.get("verifier_scorecards") or [])
        failed = [s for s in scorecards if not s.get("passed", True)]
        if failed:
            f0 = failed[0]
            return "sme_flag", (
                f"Verifier {f0.get('verifier_name','?')!r} failed: {f0.get('notes','')[:300]}"
            )

        binding = state.get("binding_verifier") or {}
        if int(binding.get("refuted", 0)) > 0:
            return "fixable", (
                f"Link-&-Binding Verifier refuted {binding['refuted']} item(s) → "
                "Arbiter for RE_EXTRACT/escalate."
            )
        if int(binding.get("uncertain", 0)) > 0:
            return "sme_flag", (
                f"Link-&-Binding Verifier returned {binding['uncertain']} uncertain "
                "item(s) → SME (unverified binding never ships)."
            )

        nrev = sum(int((r or {}).get("needs_review_count", 0)) for r in team_results.values())
        if nrev > 0:
            return "sme_flag", f"{nrev} record(s) flagged needs_review (OCR/inference) → SME."

        confs = [
            (r or {}).get("llm_confidence_score") for r in team_results.values()
            if isinstance((r or {}).get("llm_confidence_score"), (int, float))
        ]
        if confs and min(confs) < self._threshold:
            return "sme_flag", (
                f"min team confidence {min(confs):.2f} < threshold {self._threshold:.2f}"
            )

        return "auto_accept", (
            f"All {len(team_results)} team(s) committed, all verifiers passed, "
            f"no refuted/uncertain binds, confidence ≥ {self._threshold:.2f}"
        )

    # -----------------------------------------------------------------------
    # P3-M4 — partial accept: commit clean sections, escalate only the rest
    # -----------------------------------------------------------------------

    @otel_trace("decision.decision_router.decide_v3")
    def decide_v3(self, state: PipelineState) -> RouterDecisionV3:
        """Partial-accept router. Commit the clean sections; escalate ONLY the
        flagged section(s) / record(s). Verdict:
          auto_accept    — nothing flagged, no escalation items
          partial_accept — some sections accepted, some flagged/records escalated
          sme_flag       — structural schema failure (whole envelope suspect) OR
                           nothing acceptable.
        """
        team_results = state.get("team_results") or {}
        scorecards = list(state.get("verifier_scorecards") or [])
        binding = state.get("binding_verifier") or {}
        envelope = state.get("extraction") or {}

        # 1. A structural schema failure invalidates the WHOLE envelope.
        sv = next((s for s in scorecards if s.get("verifier_name") == "schema_validator"), None)
        if sv is not None and not sv.get("passed", True):
            return RouterDecisionV3(
                verdict="sme_flag",
                reason=f"Structural schema failure → whole envelope to SME: {sv.get('notes','')[:200]}",
            )

        # 2. Per-team (= per-section) accept vs flag.
        accepted: list[str] = []
        flagged: list[dict[str, Any]] = []
        for team_key, r in team_results.items():
            r = r or {}
            section = _TEAM_SECTION.get(team_key, team_key)
            reasons: list[str] = []
            if r.get("verdict") == "sme_flag":
                reasons.append("team sme_flag")
            if int(r.get("needs_review_count", 0) or 0) > 0:
                reasons.append(f"{r['needs_review_count']} needs_review record(s)")
            conf = r.get("llm_confidence_score")
            if isinstance(conf, (int, float)) and conf < self._threshold:
                reasons.append(f"confidence {float(conf):.2f} < {self._threshold:.2f}")
            (flagged.append({"section": section, "reasons": reasons}) if reasons
             else accepted.append(section))

        # 3. Record-level escalation items (the unit a human/VMAW reviews).
        items: list[dict[str, Any]] = []
        items += self._needs_review_items(envelope)                         # (a) inferred-token flags
        rf = next((s for s in scorecards if s.get("verifier_name") == "recall_floor"), None)
        if rf is not None:                                                  # (b) recall-floor LOUD misses
            for m in (rf.get("field_errors") or []):
                if m.get("strictness") == "loud":
                    items.append({"section": (m.get("field_name") or "").split(".")[0],
                                  "ref": m.get("field_name"), "kind": "recall_floor_loud",
                                  "detail": f"role {m.get('role')} present, field empty ({m.get('status')})"})
        if int(binding.get("uncertain", 0) or 0) > 0:                       # (c) unverified binds
            items.append({"section": "(binding)", "ref": None, "kind": "binding_uncertain",
                          "detail": f"{binding['uncertain']} unverified bind(s)"})
        if int(binding.get("refuted", 0) or 0) > 0:
            items.append({"section": "(binding)", "ref": None, "kind": "binding_refuted",
                          "detail": f"{binding['refuted']} refuted bind(s)"})

        # 4. Doc verdict.
        if not flagged and not items:
            return RouterDecisionV3("auto_accept",
                f"All {len(accepted)} section(s) committed; no escalations.",
                accepted_sections=accepted)
        if not accepted:
            return RouterDecisionV3("sme_flag",
                "No section could be accepted; full SME review.",
                flagged_sections=flagged, escalation_items=items)
        return RouterDecisionV3("partial_accept",
            f"{len(accepted)} section(s) accepted; {len(flagged)} flagged, "
            f"{len(items)} record-level escalation(s) → SME/VMAW.",
            accepted_sections=accepted, flagged_sections=flagged, escalation_items=items)

    @staticmethod
    def _needs_review_items(envelope: dict[str, Any]) -> list[dict[str, Any]]:
        """Walk the envelope for records flagged `needs_review: true`; one
        escalation item each, tagged with its section + a dotted ref."""
        out: list[dict[str, Any]] = []
        for section, payload in (envelope or {}).items():
            if not isinstance(payload, (dict, list)):
                continue

            def _walk(obj: Any, ref: str) -> None:
                if isinstance(obj, dict):
                    if obj.get("needs_review") is True:
                        out.append({"section": section, "ref": ref, "kind": "needs_review",
                                    "detail": obj.get("review_reason") or "inference flagged"})
                    for k, v in obj.items():
                        _walk(v, f"{ref}.{k}")
                elif isinstance(obj, list):
                    for i, v in enumerate(obj):
                        _walk(v, f"{ref}[{i}]")

            _walk(payload, section)
        return out


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


def make_decision_router_v2_node(*, router: DecisionRouter | None = None):
    """Phase 2 node — uses decide_v2 (aggregate across all active teams)."""
    router = router or DecisionRouter()

    @otel_trace("decision.decision_router.node_v2")
    async def decision_router_v2_node(state: PipelineState) -> dict[str, Any]:
        verdict, reason = router.decide_v2(state)
        logger.info(
            "decision_router_v2: doc_id=%s verdict=%s — %s",
            state.get("doc_id"), verdict, reason[:120],
        )
        return {"verdict": verdict, "verdict_reason": reason}

    return decision_router_v2_node


def make_decision_router_v3_node(*, router: DecisionRouter | None = None):
    """P3-M4 node — uses decide_v3 (partial accept). Writes `verdict` +
    `verdict_reason` (back-compat with persist/branching) AND the full
    `router_decision` (RouterDecisionV3.to_dict()) so persist can commit the
    accepted sections and route only the flagged section(s)/record(s) onward
    (graph_selfcorrecting / VMAW in M6+)."""
    router = router or DecisionRouter()

    @otel_trace("decision.decision_router.node_v3")
    async def decision_router_v3_node(state: PipelineState) -> dict[str, Any]:
        decision = router.decide_v3(state)
        logger.info(
            "decision_router_v3: doc_id=%s verdict=%s — %s "
            "(accepted=%d, flagged=%d, items=%d)",
            state.get("doc_id"), decision.verdict, decision.reason[:120],
            len(decision.accepted_sections), len(decision.flagged_sections),
            len(decision.escalation_items),
        )
        # Trace: the final verdict — refs cover the accepted + flagged sections so a
        # field's timeline lands on this last step when filtered by its section.
        from core.trace_recorder import extend_trace, record
        rec = record(
            phase="decision", agent="DecisionRouter",
            plain=f"Our system made the final call on the whole document — verdict: {decision.verdict or 'set'}.",
            refs=list(decision.accepted_sections or []) + list(decision.flagged_sections or []),
            input_summary=(f"{len(decision.accepted_sections or [])} accepted + "
                           f"{len(decision.flagged_sections or [])} flagged + "
                           f"{len(decision.escalation_items or [])} escalation item(s)"),
            output_summary=f"verdict={decision.verdict}",
            verdict=str(decision.verdict),
            reasoning=str(decision.reason or ""))
        return {
            "verdict": decision.verdict,
            "verdict_reason": decision.reason,
            "router_decision": decision.to_dict(),
            "agent_trace": extend_trace(state.get("agent_trace"), rec),
        }

    return decision_router_v3_node
