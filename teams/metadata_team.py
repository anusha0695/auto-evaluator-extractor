"""
MetadataTeam — owns schema-v2 section `report_metadata`.

Composition (orchestrated by `MetadataTeam.run()`):

    ┌─────────────────────────────────────────────────────────────────┐
    │  Extractor.invoke(state, parser_hypothesis_count=N)              │
    │     → ExtractorResult.output  (validated against ReportMetadata) │
    └────────────────────────────┬────────────────────────────────────┘
                                 ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │  CoverageAuditor.invoke(extractor_output, parser_hypothesis)     │
    │     → AuditorResult                                              │
    └────────────────────────────┬────────────────────────────────────┘
                                 │
                  ┌──────────────┼───────────────┐
                  │              │               │
              coverage_ok    coverage_ok=False   (cannot happen)
              gap_signal     OR gap_signal=True
              both false           │
                  │                ▼
                  │       Arbiter.invoke(extractor, auditor)
                  │              → ArbiterResult.policy ∈
                  │                {ACCEPT_EXTRACTOR, RE_EXTRACT, INVOKE_VMAW}
                  │                       │
                  ▼                       │
            COMMIT                        ▼
        report_metadata     ┌──────────────┴─────────────────────┐
        (verdict=committed) │                                    │
                            ▼                                    ▼
                  ACCEPT_EXTRACTOR                  RE_EXTRACT      INVOKE_VMAW
                  → COMMIT                          → re-run        → SME flag
                                                     Extractor      (verdict=sme_flag,
                                                     once, then     reason=vmaw_brief)
                                                     COMMIT or
                                                     SME flag

Caps to prevent infinite loops:
  - MAX_RE_EXTRACT_ROUNDS = 1   (Extractor runs at most twice per doc)
  - Arbiter runs at most ONCE — its policy is final

If `vmaw_available=False` (Phase 1 default), the team treats
INVOKE_VMAW as `sme_flag` per the locked Phase 1 design.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from agents import (
    Arbiter,
    ArbiterResult,
    AuditorResult,
    CoverageAuditor,
    Extractor,
    ExtractorResult,
)
from core.observability import trace as otel_trace
from core.state import PipelineState

logger = logging.getLogger(__name__)


TeamVerdict = Literal["committed", "sme_flag"]


MAX_RE_EXTRACT_ROUNDS: int = 1


@dataclass
class MetadataTeamResult:
    """The team's emitted outcome + full reasoning chain for SME audit."""

    doc_id: str
    schema_section: str = "report_metadata"

    # Committed output. None when verdict=sme_flag.
    output: dict[str, Any] | None = None

    verdict: TeamVerdict = "committed"
    verdict_reason: str = ""

    # The three agent results (Extractor may have run twice).
    extractor_result: ExtractorResult | None = None
    extractor_result_retry: ExtractorResult | None = None
    auditor_result: AuditorResult | None = None
    arbiter_result: ArbiterResult | None = None

    # Aggregates.
    total_latency_ms: int = 0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    total_tool_calls: int = 0


class MetadataTeam:
    """Composes Extractor + CoverageAuditor + Arbiter into one team."""

    def __init__(
        self,
        *,
        extractor: Extractor,
        coverage_auditor: CoverageAuditor,
        arbiter: Arbiter,
        vmaw_available: bool = False,
        team_name: str = "MetadataTeam",
        schema_section: str = "report_metadata",
    ) -> None:
        self._extractor = extractor
        self._auditor = coverage_auditor
        self._arbiter = arbiter
        self._vmaw_available = vmaw_available
        self._team_name = team_name
        self._schema_section = schema_section

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @otel_trace("teams.metadata_team.run")
    async def run(self, state: PipelineState) -> MetadataTeamResult:
        """Run the full team flow for one document. Returns MetadataTeamResult."""
        doc_id = state.get("doc_id") or "unknown"
        t0 = time.monotonic()
        out = MetadataTeamResult(doc_id=doc_id, schema_section=self._schema_section)

        # ----- Filter the parser hypothesis to this team's umbrella -------
        parser_hyp_candidates = self._candidates_for_section(state)

        # ----- 1. Extractor (first attempt) -------------------------------
        out.extractor_result = await self._extractor.invoke(
            state=state,
            parser_hypothesis_count=len(parser_hyp_candidates),
        )
        self._accumulate(out, out.extractor_result)

        # ----- 2. Coverage Auditor ----------------------------------------
        out.auditor_result = await self._auditor.invoke(
            doc_id=doc_id,
            extractor_output=out.extractor_result.output,
            parser_hypothesis_candidates=[
                dict(c) for c in parser_hyp_candidates
            ],
        )
        self._accumulate(out, out.auditor_result)

        # ----- 3. Happy path: no conflict → commit ------------------------
        if out.auditor_result.coverage_ok and not out.auditor_result.gap_signal:
            out.output = out.extractor_result.output
            out.verdict = "committed"
            out.verdict_reason = (
                "Coverage Auditor reported coverage_ok and no gap signal; "
                "committed Extractor output."
            )
            out.total_latency_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "MetadataTeam: doc_id=%s committed without Arbiter (%dms)",
                doc_id, out.total_latency_ms,
            )
            return out

        # ----- 4. Conflict → Arbiter --------------------------------------
        out.arbiter_result = await self._arbiter.invoke(
            doc_id=doc_id,
            extractor_output=out.extractor_result.output,
            auditor_output=self._auditor_output_for_arbiter(out.auditor_result),
        )
        self._accumulate(out, out.arbiter_result)

        # ----- 5. Branch on Arbiter policy --------------------------------
        policy = out.arbiter_result.policy

        if policy == "ACCEPT_EXTRACTOR":
            out.output = out.extractor_result.output
            out.verdict = "committed"
            out.verdict_reason = (
                f"Arbiter chose ACCEPT_EXTRACTOR: {out.arbiter_result.reasoning}"
            )
            out.total_latency_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "MetadataTeam: doc_id=%s Arbiter→ACCEPT_EXTRACTOR (%dms)",
                doc_id, out.total_latency_ms,
            )
            return out

        if policy == "INVOKE_VMAW":
            return self._sme_flag(
                out,
                reason=(
                    "Arbiter chose INVOKE_VMAW; VMAW is Phase 3+. Flagging to SME. "
                    f"Brief: {out.arbiter_result.vmaw_dispatch_brief}. "
                    f"Reasoning: {out.arbiter_result.reasoning}"
                )
                if not self._vmaw_available
                else (
                    "Arbiter chose INVOKE_VMAW. VMAW invocation TBD; flagging to "
                    "SME until Phase 3 wires it in."
                ),
                t0=t0,
            )

        # policy == "RE_EXTRACT" ------------------------------------------
        if not out.arbiter_result.re_extract_hints:
            # Should not happen per the arbiter.j2 contract, but defend.
            return self._sme_flag(
                out,
                reason=(
                    "Arbiter chose RE_EXTRACT but emitted no re_extract_hints — "
                    "cannot guide a useful retry. Flagging to SME."
                ),
                t0=t0,
            )

        # Re-run Extractor with hints.
        out.extractor_result_retry = await self._extractor.invoke(
            state=state,
            parser_hypothesis_count=len(parser_hyp_candidates),
            re_extract_hints=list(out.arbiter_result.re_extract_hints),
        )
        self._accumulate(out, out.extractor_result_retry)

        # We do NOT run Auditor / Arbiter again — single re-extract round only.
        # The retry output is committed as-is.
        out.output = out.extractor_result_retry.output
        out.verdict = "committed"
        out.verdict_reason = (
            f"Arbiter chose RE_EXTRACT with {len(out.arbiter_result.re_extract_hints)} "
            f"hint(s); Extractor re-ran successfully and output was committed. "
            f"Reasoning: {out.arbiter_result.reasoning}"
        )
        out.total_latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "MetadataTeam: doc_id=%s Arbiter→RE_EXTRACT, retry committed (%dms)",
            doc_id, out.total_latency_ms,
        )
        return out

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _candidates_for_section(self, state: PipelineState) -> list[dict[str, Any]]:
        """Filter `state.parser_hypothesis.candidates` to this team's umbrella."""
        ph = state.get("parser_hypothesis") or {}
        candidates = ph.get("candidates") or []
        return [
            c for c in candidates
            if (c.get("target_umbrella") if isinstance(c, dict) else None)
               == self._schema_section
        ]

    @staticmethod
    def _auditor_output_for_arbiter(auditor: AuditorResult) -> dict[str, Any]:
        """Serialize the AuditorResult into the dict shape arbiter.j2 expects."""
        return {
            "coverage_ok": auditor.coverage_ok,
            "gap_signal": auditor.gap_signal,
            "missed_fields": [
                {
                    "field_name": m.field_name,
                    "evidence_location": m.evidence_location,
                    "extracted_value_from_source": m.extracted_value_from_source,
                    "why_extractor_should_have_caught_it":
                        m.why_extractor_should_have_caught_it,
                }
                for m in auditor.missed_fields
            ],
            "spurious_fields": [
                {
                    "field_name": s.field_name,
                    "extractor_value": s.extractor_value,
                    "evidence_against": s.evidence_against,
                }
                for s in auditor.spurious_fields
            ],
            "parser_hypothesis_misses": [
                {
                    "candidate_text": p.candidate_text,
                    "candidate_label": p.candidate_label,
                    "candidate_page": p.candidate_page,
                    "should_have_landed_in": p.should_have_landed_in,
                }
                for p in auditor.parser_hypothesis_misses
            ],
            "auditor_notes": auditor.auditor_notes,
        }

    @staticmethod
    def _accumulate(out: MetadataTeamResult, agent_result: Any) -> None:
        """Roll up token usage + tool calls from an individual agent result."""
        out.total_tokens_input += int(getattr(agent_result, "tokens_input", 0) or 0)
        out.total_tokens_output += int(getattr(agent_result, "tokens_output", 0) or 0)
        out.total_tool_calls += int(getattr(agent_result, "tool_calls_made", 0) or 0)

    def _sme_flag(
        self,
        out: MetadataTeamResult,
        *,
        reason: str,
        t0: float,
    ) -> MetadataTeamResult:
        out.output = None
        out.verdict = "sme_flag"
        out.verdict_reason = reason
        out.total_latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "MetadataTeam: doc_id=%s verdict=sme_flag (%dms): %s",
            out.doc_id, out.total_latency_ms, reason[:200],
        )
        return out
