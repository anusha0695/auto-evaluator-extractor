"""
SectionTeam — the generic Phase-2 Specialist Team.

One class for ALL umbrella sections. The per-umbrella specificity lives entirely
in (a) the schema section and (b) the team's Jinja prompt — so a per-section
Python subclass would be empty. This class is the parameterized version of the
Phase-1 `MetadataTeam` orchestration:

    Extractor.invoke ─► CoverageAuditor.invoke ─► (Arbiter only on conflict)
                                                   │
                       commit ◄── ACCEPT_EXTRACTOR ┤
                       re-extract once ◄ RE_EXTRACT┤
                       sme_flag ◄──── INVOKE_VMAW ─┘   (VMAW = Phase 3)

`graph_v1` keeps using the frozen `MetadataTeam`; `graph_linear` builds every team —
including metadata — through `build_section_team(...)`.

Caps (same as MetadataTeam): Extractor runs at most twice (1 re-extract round);
Arbiter runs at most once. If `vmaw_available=False`, INVOKE_VMAW → sme_flag, and
ANY record carrying `needs_review: true` also routes to sme_flag (the OCR/VMAW
escalation carrier — see PHASE_2_VMAW_TRIGGERS.md).
"""

from __future__ import annotations

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
from core.errors import AgentError
from core.observability import trace as otel_trace
from core.prompt_renderer import PromptRenderer
from core.schema_loader import SchemaLoader
from core.state import PipelineState

logger = logging.getLogger(__name__)

TeamVerdict = Literal["committed", "sme_flag"]
MAX_RE_EXTRACT_ROUNDS: int = 1


def _camel(team_key: str) -> str:
    return "".join(p.capitalize() for p in team_key.split("_"))


@dataclass
class SectionTeamResult:
    doc_id: str
    schema_section: str
    team_name: str
    output: dict[str, Any] | None = None
    verdict: TeamVerdict = "committed"
    verdict_reason: str = ""
    needs_review_count: int = 0          # records flagged for VMAW/SME
    extractor_result: ExtractorResult | None = None
    extractor_result_retry: ExtractorResult | None = None
    auditor_result: AuditorResult | None = None
    arbiter_result: ArbiterResult | None = None
    total_latency_ms: int = 0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    total_tool_calls: int = 0
    agent_trace: list[dict[str, Any]] = field(default_factory=list)  # P3-M8b


class SectionTeam:
    """Composes Extractor + CoverageAuditor + Arbiter for one schema section."""

    def __init__(
        self,
        *,
        team_name: str,
        schema_section: str,
        extractor: Extractor,
        coverage_auditor: CoverageAuditor,
        arbiter: Arbiter,
        vmaw_available: bool = False,
    ) -> None:
        self._team_name = team_name
        self._schema_section = schema_section
        self._extractor = extractor
        self._auditor = coverage_auditor
        self._arbiter = arbiter
        self._vmaw_available = vmaw_available

    @property
    def schema_section(self) -> str:
        return self._schema_section

    @property
    def team_name(self) -> str:
        return self._team_name

    @otel_trace("teams.section_team.run")
    async def run(
        self, state: PipelineState, *, repair_hints: list[dict[str, str]] | None = None
    ) -> SectionTeamResult:
        doc_id = state.get("doc_id") or "unknown"
        t0 = time.monotonic()
        out = SectionTeamResult(
            doc_id=doc_id, schema_section=self._schema_section, team_name=self._team_name,
        )

        candidates = self._candidates_for_section(state)

        # P3-M6 focused repair pass: a single hinted, fix-or-drop re-extract with
        # the prior output as the base. The graph_selfcorrecting re-verify (linker→verifiers→
        # triage) is the backstop, so the auditor/arbiter round is skipped here.
        if repair_hints:
            out.extractor_result = await self._extractor.invoke(
                state=state, parser_hypothesis_count=len(candidates),
                re_extract_hints=list(repair_hints),
            )
            self._accumulate(out, out.extractor_result)
            return self._commit(
                out, out.extractor_result.output,
                reason=f"P3-M6 focused repair: {len(repair_hints)} hint(s); re-extracted.",
                t0=t0,
            )

        out.extractor_result = await self._extractor.invoke(
            state=state, parser_hypothesis_count=len(candidates),
        )
        self._accumulate(out, out.extractor_result)

        out.auditor_result = await self._auditor.invoke(
            doc_id=doc_id,
            extractor_output=out.extractor_result.output,
            parser_hypothesis_candidates=[dict(c) for c in candidates],
        )
        self._accumulate(out, out.auditor_result)

        # Happy path → commit (subject to needs_review escalation).
        if out.auditor_result.coverage_ok and not out.auditor_result.gap_signal:
            return self._commit(
                out, out.extractor_result.output,
                reason="Coverage Auditor reported coverage_ok and no gap signal.",
                t0=t0,
            )

        # Conflict → Arbiter (at most once).
        out.arbiter_result = await self._arbiter.invoke(
            doc_id=doc_id,
            extractor_output=out.extractor_result.output,
            auditor_output=self._auditor_output_for_arbiter(out.auditor_result),
        )
        self._accumulate(out, out.arbiter_result)
        policy = out.arbiter_result.policy

        if policy == "ACCEPT_EXTRACTOR":
            return self._commit(
                out, out.extractor_result.output,
                reason=f"Arbiter ACCEPT_EXTRACTOR: {out.arbiter_result.reasoning}",
                t0=t0,
            )

        if policy == "INVOKE_VMAW":
            return self._sme_flag(
                out,
                reason=(
                    "Arbiter INVOKE_VMAW; VMAW is Phase 3 — flagging to SME. "
                    f"Brief: {out.arbiter_result.vmaw_dispatch_brief}. "
                    f"Reasoning: {out.arbiter_result.reasoning}"
                ),
                t0=t0,
            )

        # RE_EXTRACT
        if not out.arbiter_result.re_extract_hints:
            return self._sme_flag(
                out,
                reason="Arbiter RE_EXTRACT but emitted no hints — flagging to SME.",
                t0=t0,
            )
        # R1: resilient retry. The retry extractor call CAN fail hard — most
        # commonly with `AgentError("Extractor returned an empty final message")`
        # when Gemini returns empty content twice in a row on the correction
        # round-trip. Pre-R1 this crashed the entire pipeline run, throwing
        # away the FIRST-PASS extraction (which was valid). The first pass is
        # already committed on `out.extractor_result.output` — fall back to it
        # so the pipeline continues. The downstream verifier + triage + VMAW
        # path handles any remaining gaps. The auditor's hint set is preserved
        # in the trace for future analysis.
        try:
            out.extractor_result_retry = await self._extractor.invoke(
                state=state,
                parser_hypothesis_count=len(candidates),
                re_extract_hints=list(out.arbiter_result.re_extract_hints),
            )
            self._accumulate(out, out.extractor_result_retry)
            return self._commit(
                out, out.extractor_result_retry.output,
                reason=(
                    f"Arbiter RE_EXTRACT with {len(out.arbiter_result.re_extract_hints)} "
                    f"hint(s); retry committed."
                ),
                t0=t0,
            )
        except AgentError as exc:  # noqa: BLE001 — degrade gracefully
            logger.warning(
                "SectionTeam[%s]: doc_id=%s retry-extractor failed (%s) — "
                "falling back to FIRST-PASS extraction (R1 resilient retry). "
                "Downstream verifier / triage / VMAW will handle remaining gaps.",
                self._team_name, doc_id, exc,
            )
            return self._commit(
                out, out.extractor_result.output,
                reason=(
                    f"Arbiter RE_EXTRACT with {len(out.arbiter_result.re_extract_hints)} "
                    f"hint(s); retry failed ({exc}); FIRST-PASS retained (R1)."
                ),
                t0=t0,
            )

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _candidates_for_section(self, state: PipelineState) -> list[dict[str, Any]]:
        ph = state.get("parser_hypothesis") or {}
        candidates = ph.get("candidates") or []
        return [
            c for c in candidates
            if (c.get("target_umbrella") if isinstance(c, dict) else None)
               == self._schema_section
        ]

    @staticmethod
    def _count_needs_review(output: dict[str, Any] | None) -> int:
        """Count records flagged `needs_review: true` anywhere in the section
        output (the OCR/VMAW escalation carrier)."""
        if not output:
            return 0
        n = 0
        def _walk(obj: Any) -> None:
            nonlocal n
            if isinstance(obj, dict):
                if obj.get("needs_review") is True:
                    n += 1
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    _walk(v)
        _walk(output)
        return n

    def _commit(
        self, out: SectionTeamResult, output: dict[str, Any], *, reason: str, t0: float,
    ) -> SectionTeamResult:
        out.needs_review_count = self._count_needs_review(output)
        if out.needs_review_count > 0 and not self._vmaw_available:
            # Inference-based corrections must be verified — route to SME until
            # VMAW exists (PHASE_2_VMAW_TRIGGERS.md).
            return self._sme_flag(
                out,
                reason=(
                    f"{reason} BUT {out.needs_review_count} record(s) carry "
                    f"needs_review=true (OCR/inference correction) → SME until VMAW."
                ),
                t0=t0, output_for_audit=output,
            )
        out.output = output
        out.verdict = "committed"
        out.verdict_reason = reason
        out.total_latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info("SectionTeam[%s]: doc_id=%s committed (%dms, needs_review=%d)",
                    self._team_name, out.doc_id, out.total_latency_ms, out.needs_review_count)
        return out

    def _sme_flag(
        self, out: SectionTeamResult, *, reason: str, t0: float,
        output_for_audit: dict[str, Any] | None = None,
    ) -> SectionTeamResult:
        out.output = None
        out.verdict = "sme_flag"
        out.verdict_reason = reason
        out.total_latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info("SectionTeam[%s]: doc_id=%s verdict=sme_flag (%dms): %s",
                    self._team_name, out.doc_id, out.total_latency_ms, reason[:200])
        return out

    @staticmethod
    def _auditor_output_for_arbiter(auditor: AuditorResult) -> dict[str, Any]:
        return {
            "coverage_ok": auditor.coverage_ok,
            "gap_signal": auditor.gap_signal,
            "missed_fields": [
                {
                    "field_name": m.field_name,
                    "evidence_location": m.evidence_location,
                    "extracted_value_from_source": m.extracted_value_from_source,
                    "why_extractor_should_have_caught_it": m.why_extractor_should_have_caught_it,
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
    def _accumulate(out: SectionTeamResult, agent_result: Any) -> None:
        out.total_tokens_input += int(getattr(agent_result, "tokens_input", 0) or 0)
        out.total_tokens_output += int(getattr(agent_result, "tokens_output", 0) or 0)
        out.total_tool_calls += int(getattr(agent_result, "tool_calls_made", 0) or 0)


# ---------------------------------------------------------------------------
# Factory — build any team from teams.yaml
# ---------------------------------------------------------------------------


def build_section_team(
    team_key: str,
    *,
    teams_cfg: dict[str, Any],
    schema_loader: SchemaLoader,
    prompt_renderer: PromptRenderer,
    tools_yaml_path: str = "config/tools.yaml",
    pipeline_version: str = "v2",
    vmaw_available: bool = False,
) -> SectionTeam:
    """Construct a SectionTeam (Extractor + CoverageAuditor + Arbiter) for the
    named team key in `teams.yaml`. Works for every team, metadata included."""
    cfg = teams_cfg["teams"][team_key]
    team_name = _camel(team_key)
    section = cfg["schema_section"]
    models = cfg["models"]

    extractor = Extractor(
        team_name=team_name,
        schema_section=section,
        team_prompt_template=cfg["prompt_template"],
        tool_allowlist=list(cfg["tool_allowlist"]),
        prompt_renderer=prompt_renderer,
        schema_loader=schema_loader,
        pipeline_version=pipeline_version,
        tools_yaml_path=tools_yaml_path,
        model_name=models["extractor"]["name"],
        temperature=float(models["extractor"]["temperature"]),
    )
    auditor = CoverageAuditor(
        team_name=team_name,
        schema_section=section,
        prompt_renderer=prompt_renderer,
        coverage_gap_tolerance=float(cfg.get("coverage_gap_tolerance", 0.10)),
        model_name=models["coverage_auditor"]["name"],
        temperature=float(models["coverage_auditor"]["temperature"]),
    )
    arbiter = Arbiter(
        team_name=team_name,
        schema_section=section,
        prompt_renderer=prompt_renderer,
        pipeline_version=pipeline_version,
        vmaw_available=vmaw_available,
        model_name=models["arbiter"]["name"],
        temperature=float(models["arbiter"]["temperature"]),
    )
    return SectionTeam(
        team_name=team_name,
        schema_section=section,
        extractor=extractor,
        coverage_auditor=auditor,
        arbiter=arbiter,
        vmaw_available=vmaw_available,
    )
