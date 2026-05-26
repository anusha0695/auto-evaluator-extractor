"""
graph_linear — Phase 2a LangGraph composition.

Flow:

    document_received_v2 ─► preprocess ─► planner ─► teams (active, ∥)
        ─► linker ─► verifiers ─► decision_router_v2 ─► persist ─► END

`graph_v1` is untouched and frozen. `pipeline/runner.py` dispatches by
`--version`. Schema v3 is loaded here (graph_v1 stays on v2).

The post-team logic is factored into two PURE helpers (`assemble_and_link`,
`run_verifier_suite`) so the deterministic core is unit-testable without cloud
or LLM. The Linker/Verifier LLM adjudicators (contextual MERGE/LINK, V2/V4) are
dependency-injected; when absent the deterministic path runs and ambiguous cases
escalate (→ SME) per PHASE_2_VMAW_TRIGGERS.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agents.link_binding_verifier import LinkBindingVerifier
from agents.linker import Linker
from agents.planner import Planner
from core.errors import PipelineError
from core.observability import trace as otel_trace
from core.schema_loader import SchemaLoader
from core.state import PipelineState
from decision.decision_router import DecisionRouter, make_decision_router_v2_node
from verification.core_verifiers import (
    CoverageVerifier,
    EvidenceConfidenceVerifier,
    LinkConsistencyVerifier,
)
from verification.schema_validator import SchemaValidator

logger = logging.getLogger(__name__)

# The teams graph_linear manages (metadata always-active; the rest gated by the
# Planner). Includes the Phase-2b SpecimenFindingsTeam — activated only when a
# significant_findings signal is present (skipped on molecular-only reports).
PHASE_2A_TEAM_KEYS = [
    "metadata_team",
    "molecular_biomarker_team",
    "tested_biomarker_team",
    "clinical_info_team",
    "specimen_findings_team",
]


@dataclass
class GraphDependencies:
    storage_config: Any
    schema_loader: SchemaLoader
    prompt_renderer: Any
    persistence: Any
    preprocess_deps: Any
    planner: Planner
    teams: dict[str, Any]                  # team_key → SectionTeam
    linker: Linker
    binding_verifier: LinkBindingVerifier
    decision_router: DecisionRouter
    coverage_gap_tolerance: float = 0.10
    # P3-M10: Gemini-backed semantic hooks (None → advisory / SME, gates stay offline).
    attribution_fn: Any = None             # owner-keyed attribution check
    vmaw_hooks: dict[str, Any] | None = None   # {expand_context_fn, cite_fn, adjudicate_value_fn}
    recall_reread_fn: Any = None           # recall-floor AI re-read (true-absence vs real miss)
    normalizers: dict[str, Any] | None = None  # gap #2: {key → adapter} for renormalize_field
    triage_llm: Any = None                 # gap #7: triage tie-break (pick a team for an unteamable repair)


def build_graph_dependencies(
    *,
    storage_config: Any | None = None,
    schema_path: str = "config/schemas/genomic_pathology_v3.json",
    prompts_root: str = "config/prompts",
    tools_yaml_path: str = "config/tools.yaml",
    teams_yaml_path: str = "config/teams.yaml",
    team_keys: list[str] | None = None,
    # Optional LLM adjudicators (graph_linear runs deterministic + escalate if None):
    merge_adjudicator: Callable | None = None,
    link_adjudicator: Callable | None = None,
    supersession_resolver: Callable | None = None,
    relationship_confirm: Callable | None = None,
    link_confirm: Callable | None = None,
) -> GraphDependencies:
    import yaml

    from core.persistence import Persistence, load_storage_config
    from core.prompt_renderer import PromptRenderer
    from preprocess.block_profiler import BlockProfiler
    from preprocess.docai_parser import DocAIParser
    from preprocess.fax_header_filter import FaxHeaderFilter
    from preprocess.medical_ner import SciSpaCyMedicalNER
    from preprocess.preprocess_node import PreprocessNodeDependencies
    from teams import build_section_team

    cfg = storage_config or load_storage_config("phase_2")
    schema_loader = SchemaLoader.from_path(schema_path)
    prompt_renderer = PromptRenderer(schema_loader=schema_loader, prompts_root=prompts_root)
    persistence = Persistence(cfg)

    docai = DocAIParser(persistence=persistence)
    preprocess_deps = PreprocessNodeDependencies(
        docai_parser=docai,
        fax_filter=FaxHeaderFilter(),
        block_profiler=BlockProfiler(prompt_renderer=prompt_renderer),
        medical_ner=SciSpaCyMedicalNER(prompt_renderer=prompt_renderer),
        persistence=persistence,
    )

    # LLM adjudicators: if the caller didn't inject any and they're enabled,
    # build the Gemini-backed set (lazy — no network until first call). This is
    # the contextual MERGE/LINK/relationship-confirm layer; absent them the
    # Linker/Verifier run deterministic-only and escalate ambiguity.
    if (merge_adjudicator is None and link_adjudicator is None and supersession_resolver is None
            and relationship_confirm is None and link_confirm is None):
        from agents.adjudicators import build_llm_adjudicators, llm_adjudicators_enabled
        if llm_adjudicators_enabled():
            _adj = build_llm_adjudicators()
            merge_adjudicator = _adj["merge_adjudicator"]
            link_adjudicator = _adj["link_adjudicator"]
            supersession_resolver = _adj["supersession_resolver"]
            relationship_confirm = _adj["relationship_confirm"]
            link_confirm = _adj["link_confirm"]
            logger.info("graph_linear: LLM adjudicators ENABLED (set LLM_ADJUDICATORS=0 to disable)")
        else:
            logger.info("graph_linear: LLM adjudicators disabled via env — deterministic-only Linker/Verifier")

    teams_cfg = yaml.safe_load(open(teams_yaml_path, encoding="utf-8"))
    keys = team_keys or PHASE_2A_TEAM_KEYS
    teams = {
        k: build_section_team(
            k, teams_cfg=teams_cfg, schema_loader=schema_loader,
            prompt_renderer=prompt_renderer, tools_yaml_path=tools_yaml_path,
            pipeline_version="v2", vmaw_available=False,
        )
        for k in keys
    }

    # P3-M5: typed link registry (Tier 1+2) + deterministic-seed toggle.
    # LINK_SEED=0/false/no flips to the pure-contextual A/B arm (seed hints only).
    from agents.link_registry import LinkRegistry
    try:
        _link_registry = LinkRegistry.from_path("config/link_registry.yaml", max_tier=2)
    except Exception as exc:  # noqa: BLE001 — degrade to legacy validation
        logger.warning("graph_linear: link registry unavailable (%s) → legacy link validation", exc)
        _link_registry = None
    _link_seed_enabled = os.environ.get("LINK_SEED", "1").lower() not in ("0", "false", "no", "off")

    linker = Linker(
        merge_adjudicator=merge_adjudicator,
        link_adjudicator=link_adjudicator,
        supersession_resolver=supersession_resolver,
        link_registry=_link_registry,
        link_seed_enabled=_link_seed_enabled,
    )
    binding_verifier = LinkBindingVerifier(
        relationship_confirm=relationship_confirm, link_confirm=link_confirm,
    )

    # P3-M10: Gemini-backed attribution hook + VMAW EC/CITE/VA hooks (same env gate
    # + lazy construction as the adjudicators; None → advisory/SME, gates stay offline).
    # gap #2: deterministic normalizer adapters (offline; always available — not LLM-gated).
    _normalizers = None
    try:
        from agents.normalizer_hooks import build_normalizers
        _normalizers = build_normalizers()
    except Exception:  # noqa: BLE001
        logger.exception("graph_linear: failed to build normalizers (non-fatal)")

    _attribution_fn = None
    _vmaw_hooks = None
    _recall_reread_fn = None
    _triage_llm = None
    try:
        from agents.adjudicators import (build_attribution_fn, build_recall_reread_fn,
                                         build_triage_llm, build_vmaw_hooks, llm_adjudicators_enabled)
        if llm_adjudicators_enabled():
            _attribution_fn = build_attribution_fn()
            _vmaw_hooks = build_vmaw_hooks()
            _recall_reread_fn = build_recall_reread_fn()
            _triage_llm = build_triage_llm()
    except Exception:  # noqa: BLE001 — degrade to advisory/SME
        logger.exception("graph_linear: failed to build M10 semantic hooks (non-fatal)")

    return GraphDependencies(
        storage_config=cfg, schema_loader=schema_loader, prompt_renderer=prompt_renderer,
        persistence=persistence, preprocess_deps=preprocess_deps, planner=Planner(),
        teams=teams, linker=linker, binding_verifier=binding_verifier,
        decision_router=DecisionRouter(),
        attribution_fn=_attribution_fn, vmaw_hooks=_vmaw_hooks, recall_reread_fn=_recall_reread_fn,
        normalizers=_normalizers, triage_llm=_triage_llm,
    )


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable; no cloud/LLM)
# ---------------------------------------------------------------------------


def assemble_and_link(
    *,
    linker: Linker,
    section_outputs: dict[str, dict[str, Any]],
    blocks: list[dict[str, Any]],
    block_profiles: list[dict[str, Any]],
    relink_hints: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[Any], list[dict[str, str]]]:
    """Assemble the v3 envelope + cross-section links. Returns
    (envelope, links, needs_review_refs). `relink_hints` (gap #3) carry refuted
    pairings into the adjudicator so a re-link re-evaluates instead of re-emitting."""
    res = linker.link(sections=section_outputs, blocks=blocks, block_profiles=block_profiles,
                      relink_hints=relink_hints)
    return res.envelope, res.links, res.needs_review_refs


def run_verifier_suite(
    *,
    schema_loader: SchemaLoader,
    binding_verifier: LinkBindingVerifier,
    envelope: dict[str, Any],
    links: list[Any],
    blocks: list[dict[str, Any]],
    parser_hypothesis: dict[str, Any],
    gap_tolerance: float = 0.10,
    block_profiles: list[dict[str, Any]] | None = None,
    recall_reread_fn: Any | None = None,
    block_reads: dict[str, Any] | None = None,
    binding_items_out: list[dict[str, Any]] | None = None,
    attribution_fn: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Run schema + the deterministic verifiers (incl. P3-M3 recall floor) + the
    binding verifier. Returns (scorecards, binding_summary).

    P3-M6: `recall_reread_fn` (+ memoized `block_reads`) activates the recall-floor
    AI re-read so a LOUD miss is confirmed present (→ ping-back) vs genuinely absent
    (→ accept). Both default None → the M3 advisory behavior (no regression)."""
    scorecards: list[dict[str, Any]] = []

    _passed_raw, errors = SchemaValidator(schema_loader=schema_loader).validate_envelope(envelope)
    # Split STRUCTURAL (shape/type/required/unknown-key — hard fail) from
    # COSMETIC (pattern/format/min/max — quality notes, non-fatal). The teams
    # validate with Pydantic (no pattern/format/min), so a cosmetic jsonschema
    # nit must not sink a doc that the teams already committed.
    _COSMETIC = {"pattern", "format", "minimum", "maximum", "minLength",
                 "maxLength", "exclusiveMinimum", "exclusiveMaximum"}
    structural = [e for e in errors if str(e.get("type", "")) not in _COSMETIC]
    cosmetic = [e for e in errors if str(e.get("type", "")) in _COSMETIC]
    sv_passed = not structural
    sv_notes = ("v3 envelope structurally valid" if sv_passed
                else f"{len(structural)} structural schema error(s)")
    if cosmetic:
        sv_notes += f"; {len(cosmetic)} format/pattern note(s) (non-fatal)"
    scorecards.append({
        "verifier_name": "schema_validator", "passed": sv_passed,
        "field_errors": structural[:25], "cosmetic_notes": cosmetic[:25],
        "notes": sv_notes,
    })
    scorecards.append(CoverageVerifier(gap_tolerance=gap_tolerance).verify(envelope, parser_hypothesis))
    scorecards.append(LinkConsistencyVerifier().verify(envelope, links))
    scorecards.append(EvidenceConfidenceVerifier().verify(envelope))

    # P3-M3: block-role recall floor (SECTION/FIELD-level recall net for the
    # narrative sections). Advisory in M3 — the AI re-read (reread_fn) + the loud
    # hard-gating get wired when the ping-back loop (M6) lands. Defensive: a
    # recall-floor failure must never sink the suite.
    try:
        from verification.recall_floor import RecallFloorVerifier
        scorecards.append(RecallFloorVerifier(reread_fn=recall_reread_fn).verify(
            envelope=envelope, block_profiles=block_profiles or [],
            block_reads=block_reads))
    except Exception:
        logger.exception("run_verifier_suite: recall_floor verifier failed (non-fatal)")

    # P3-M10b: owner-keyed attribution floor (does each attribute describe its
    # object's owner — e.g. is this laterality really specimen A's?). Advisory in
    # M10b — `attribution_fn` is the injected semantic check (None → 'unverified');
    # M10c routes contested attributions into repair→VMAW→SME. Defensive.
    try:
        from verification.attribution import AttributionVerifier
        scorecards.append(AttributionVerifier(attribution_fn=attribution_fn).verify(
            envelope=envelope, blocks=blocks))
    except Exception:
        logger.exception("run_verifier_suite: attribution verifier failed (non-fatal)")

    # gap #2: deterministic normalization floor (canonical differs from extracted →
    # renormalize_field). Builds its own offline normalizers; advisory. Defensive.
    try:
        from verification.normalization import NormalizationVerifier
        scorecards.append(NormalizationVerifier().verify(envelope=envelope))
    except Exception:
        logger.exception("run_verifier_suite: normalization verifier failed (non-fatal)")

    vres = binding_verifier.verify(
        envelope=envelope, links=links, blocks=blocks, parser_hypothesis=parser_hypothesis,
    )
    binding_summary = {"refuted": len(vres.refuted), "uncertain": len(vres.uncertain)}
    # P3-M8: optionally surface the per-verdict items (ref/check/verdict/evidence)
    # for the SME trace's "why". Off by default → binding_summary stays exactly
    # {refuted, uncertain} (the m8 gate's contract); only the v3 verifier opts in.
    if binding_items_out is not None:
        for v in (vres.refuted + vres.uncertain):
            binding_items_out.append({
                "ref": getattr(v, "ref", ""), "check": getattr(v, "check", ""),
                "verdict": getattr(v, "verdict", ""), "evidence": getattr(v, "evidence", "")})
    return scorecards, binding_summary


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@otel_trace("pipeline.graph_linear.document_received")
async def _document_received_v2_node(state: PipelineState) -> dict[str, Any]:
    missing = [k for k in ("doc_id", "pipeline_version") if not state.get(k)]
    if not state.get("gcs_uri") and not state.get("raw_pdf_bytes"):
        missing.append("gcs_uri or raw_pdf_bytes")
    if missing:
        raise PipelineError(
            f"document_received_v2: missing state field(s): {missing}",
            doc_id=state.get("doc_id"), retry_safe=False,
        )
    if state.get("pipeline_version") != "v2":
        raise PipelineError(
            f"graph_linear invoked with pipeline_version={state.get('pipeline_version')!r}; expected 'v2'",
            doc_id=state.get("doc_id"), retry_safe=False,
        )
    return {"team_outputs": {}, "verifier_scorecards": [], "preprocessing_errors": [],
            "latency_ms": 0, "cost_usd": 0.0}


def _make_planner_node(*, planner: Planner, teams_cfg: dict[str, Any], team_keys: list[str]):
    @otel_trace("pipeline.graph_linear.planner_node")
    async def planner_node(state: PipelineState) -> dict[str, Any]:
        plan = planner.plan(state, teams_cfg=teams_cfg, team_keys=team_keys)
        return {"active_team_keys": plan.active_team_keys,
                "planner_rationale": plan.rationale}
    return planner_node


def _make_teams_node(*, teams: dict[str, Any]):
    @otel_trace("pipeline.graph_linear.teams_node")
    async def teams_node(state: PipelineState) -> dict[str, Any]:
        active = state.get("active_team_keys") or list(teams.keys())
        active = [k for k in active if k in teams]
        results = await asyncio.gather(*[teams[k].run(state) for k in active])

        section_outputs: dict[str, Any] = {}
        team_outputs = dict(state.get("team_outputs") or {})
        team_results: dict[str, Any] = {}
        agent_trace: list[dict[str, Any]] = []
        for key, r in zip(active, results):
            team_results[key] = {
                "verdict": r.verdict,
                "llm_confidence_score": (r.output or {}).get("llm_confidence_score") if r.output else None,
                "needs_review_count": getattr(r, "needs_review_count", 0),
            }
            if r.output is not None:
                section_outputs[r.schema_section] = r.output
                team_outputs[key] = r.output
            # P3-M8b: per-agent trace (PHI-safe summaries) for the SME UI.
            try:
                from pipeline.agent_trace import build_team_trace
                agent_trace.extend(build_team_trace(r, team_key=key))
            except Exception:
                logger.exception("teams_node: agent-trace assembly failed (non-fatal)")
        return {"section_outputs": section_outputs, "team_outputs": team_outputs,
                "team_results": team_results, "agent_trace": agent_trace}
    return teams_node


def _make_linker_node(*, linker: Linker, persistence: Any = None):
    @otel_trace("pipeline.graph_linear.linker_node")
    async def linker_node(state: PipelineState) -> dict[str, Any]:
        dp = state.get("doc_profile") or {}
        envelope, links, nrev = assemble_and_link(
            linker=linker,
            section_outputs=state.get("section_outputs") or {},
            blocks=dp.get("blocks") or [],
            block_profiles=dp.get("block_profiles") or [],
            relink_hints=state.get("relink_hints") or [],
        )
        links_dicts = [vars(l) if not isinstance(l, dict) else l for l in links]
        # Persist the assembled envelope EVERY run (not only auto_accept) so the
        # UI / SME can review it regardless of verdict.
        if persistence is not None:
            try:
                await persistence.write_artifact(
                    doc_id=state.get("doc_id") or "unknown",
                    kind="extraction_v2", content=json.dumps(envelope, indent=2, default=str))
            except Exception:
                logger.exception("linker_node: failed to persist extraction_v2 artifact")
        return {"extraction": envelope, "links": links_dicts, "link_needs_review": nrev}
    return linker_node


def _make_verifier_node(*, schema_loader: SchemaLoader, binding_verifier: LinkBindingVerifier,
                        gap_tol: float, persistence: Any = None, attribution_fn: Any = None):
    @otel_trace("pipeline.graph_linear.verifier_node")
    async def verifier_node(state: PipelineState) -> dict[str, Any]:
        dp = state.get("doc_profile") or {}
        envelope = state.get("extraction") or {}
        # rebuild Link-like objects not needed; verifiers accept dicts too.
        links = state.get("links") or []
        scorecards, binding_summary = run_verifier_suite(
            schema_loader=schema_loader, binding_verifier=binding_verifier,
            envelope=envelope, links=links, blocks=dp.get("blocks") or [],
            parser_hypothesis=state.get("parser_hypothesis") or {}, gap_tolerance=gap_tol,
            block_profiles=dp.get("block_profiles") or [], attribution_fn=attribution_fn,
        )
        existing = list(state.get("verifier_scorecards") or [])
        # Persist a PHI-safe verification artifact (scorecards + binding + links +
        # planner rationale) for the UI / SME, every run.
        if persistence is not None:
            try:
                safe_cards = [{
                    "verifier_name": s.get("verifier_name"), "passed": s.get("passed"),
                    "notes": s.get("notes", ""),
                    # verifiers key the ref differently (schema=loc, recall_floor=field_name,
                    # attribution=ref) — keep whichever is present so the SME trace can pin it.
                    "error_locs": [(e.get("loc") or e.get("field_name") or e.get("ref"))
                                   for e in (s.get("field_errors") or [])][:50],
                    "cosmetic_locs": [e.get("loc") for e in (s.get("cosmetic_notes") or [])][:50],
                } for s in (existing + scorecards)]
                await persistence.write_artifact(
                    doc_id=state.get("doc_id") or "unknown", kind="verification_v2",
                    content=json.dumps({
                        "scorecards": safe_cards, "binding_verifier": binding_summary,
                        "links": links, "planner_rationale": state.get("planner_rationale"),
                        "team_results": state.get("team_results"),
                    }, indent=2, default=str))
            except Exception:
                logger.exception("verifier_node: failed to persist verification_v2 artifact")
        return {"verifier_scorecards": existing + scorecards,
                "binding_verifier": binding_summary}
    return verifier_node


def _make_linear_persist_node(*, persistence: Any):
    @otel_trace("pipeline.graph_linear.persist_node")
    async def persist_node(state: PipelineState) -> dict[str, Any]:
        try:
            run_id = await persistence.write_run(state)
        except Exception:
            logger.exception("persist_node_v2: write_run failed for %s", state.get("doc_id"))
            run_id = None
        extraction_id = None
        if state.get("verdict") == "auto_accept" and state.get("extraction"):
            try:
                extraction_id = await persistence.write_extraction(state, run_id=run_id)
            except Exception:
                logger.exception("persist_node_v2: write_extraction failed for %s", state.get("doc_id"))
        return {"bigquery_run_row_id": run_id, "bigquery_extraction_row_id": extraction_id}
    return persist_node


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


@otel_trace("pipeline.graph_linear.build")
def build_linear_graph(
    deps: GraphDependencies,
    *,
    teams_cfg: dict[str, Any] | None = None,
    checkpointer: Any | None = None,
) -> Any:
    from langgraph.graph import END, StateGraph

    from preprocess.preprocess_node import make_preprocess_node

    if teams_cfg is None:
        import yaml
        teams_cfg = yaml.safe_load(open("config/teams.yaml", encoding="utf-8"))
    team_keys = list(deps.teams.keys())

    g = StateGraph(PipelineState)
    g.add_node("document_received", _document_received_v2_node)
    g.add_node("preprocess", make_preprocess_node(deps.preprocess_deps))
    g.add_node("planner", _make_planner_node(planner=deps.planner, teams_cfg=teams_cfg, team_keys=team_keys))
    g.add_node("teams", _make_teams_node(teams=deps.teams))
    g.add_node("linker", _make_linker_node(linker=deps.linker, persistence=deps.persistence))
    g.add_node("verifiers", _make_verifier_node(
        schema_loader=deps.schema_loader, binding_verifier=deps.binding_verifier,
        gap_tol=deps.coverage_gap_tolerance, persistence=deps.persistence,
        attribution_fn=getattr(deps, "attribution_fn", None)))
    g.add_node("decision_router", make_decision_router_v2_node(router=deps.decision_router))
    g.add_node("persist", _make_linear_persist_node(persistence=deps.persistence))

    g.set_entry_point("document_received")
    g.add_edge("document_received", "preprocess")
    g.add_edge("preprocess", "planner")
    g.add_edge("planner", "teams")
    g.add_edge("teams", "linker")
    g.add_edge("linker", "verifiers")
    g.add_edge("verifiers", "decision_router")
    g.add_edge("decision_router", "persist")
    g.add_edge("persist", END)

    compiled = g.compile(checkpointer=checkpointer)
    logger.info("graph_linear: compiled (%d teams)", len(deps.teams))
    return compiled
