"""
graph_selfcorrecting — the scoped ping-back / repair loop (P3-M6, Topic 1).

Topology (additive; graph_linear is untouched and still shipping):

    document_received → preprocess → planner → teams → linker → verifiers → triage
                                                          ↑                     │
                                              repair ─────┘   ┌──(repair)───────┤
                                                ↑─────────────┘                 │
                                                              └──(done)──→ decision_router_v3 → persist_v3

The cycle `triage → repair → linker → verifiers → triage` self-fixes addressable
defects (schema/recall-miss-present/missing-provenance/binding-refuted-once)
within a layered budget; ambiguous/exhausted/recurred defects escalate. Two extra
nodes vs graph_linear (`triage`, `repair`) + one conditional edge. Termination is
structural (recur-guard + per-team cap + global cap), with LangGraph's
`recursion_limit` as the hard backstop.

graph_selfcorrecting reuses graph_linear's shared node factories (planner/teams/linker) and the
pure `run_verifier_suite`; it overrides `document_received` (accepts v3),
`verifiers` (replaces scorecards each cycle + threads the recall-floor re-read +
`block_reads`), the router (`decide_v3`), and `persist` (commits on
auto_accept OR partial_accept + writes the repair ledger / escalation queue).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.observability import trace as otel_trace
from core.state import PipelineState
from decision.decision_router import make_decision_router_v3_node
from pipeline.graph_linear import (
    GraphDependencies,
    _make_linker_node,
    _make_planner_node,
    _make_teams_node,
    run_verifier_suite,
)
from pipeline.repair import RepairExecutor, make_repair_node
from pipeline.triage import TriageAgent, make_triage_node, triage_route
from pipeline.vmaw import VMAWAgent, make_vmaw_node

logger = logging.getLogger(__name__)

# Hard backstop for the repair loop — passed at invoke time:
#   graph.invoke(state, config={"recursion_limit": GRAPH_RECURSION_LIMIT})
# The recur-guard + budget caps terminate well before this; it only guards bugs.
GRAPH_RECURSION_LIMIT = 60


@otel_trace("pipeline.graph_selfcorrecting.document_received")
async def _document_received_v3_node(state: PipelineState) -> dict[str, Any]:
    from core.errors import PipelineError
    missing = [k for k in ("doc_id", "pipeline_version") if not state.get(k)]
    if not state.get("gcs_uri") and not state.get("raw_pdf_bytes"):
        missing.append("gcs_uri or raw_pdf_bytes")
    if missing:
        raise PipelineError(f"document_received_v3: missing state field(s): {missing}",
                            doc_id=state.get("doc_id"), retry_safe=False)
    if state.get("pipeline_version") not in ("v3", "v4"):
        raise PipelineError(
            f"graph_selfcorrecting invoked with pipeline_version={state.get('pipeline_version')!r}; "
            f"expected 'v3' or 'v4' (v4 reuses the self-correcting graph + a 4-section registry)",
            doc_id=state.get("doc_id"), retry_safe=False)
    # seed the repair-loop channels so they exist from cycle 0.
    return {"team_outputs": {}, "verifier_scorecards": [], "preprocessing_errors": [],
            "latency_ms": 0, "cost_usd": 0.0,
            "repair_budget_used": 0, "repair_requests": [], "defect_signatures_seen": [],
            "block_reads": {}, "repair_log": [], "escalation_queue": []}


def _make_selfcorrecting_verifier_node(*, schema_loader, binding_verifier, gap_tol,
                           recall_reread_fn=None, persistence=None, attribution_fn=None,
                           disabled_sections=None):
    """Like graph_linear's verifier node but loop-aware: REPLACES the scorecards each
    cycle (not append — stale cards would mislead triage), threads the recall-floor
    re-read + the memoized `block_reads`, and returns the updated cache."""
    @otel_trace("pipeline.graph_selfcorrecting.verifier_node")
    async def verifier_node(state: PipelineState) -> dict[str, Any]:
        dp = state.get("doc_profile") or {}
        envelope = state.get("extraction") or {}
        links = state.get("links") or []
        block_reads = dict(state.get("block_reads") or {})
        binding_items: list = []
        blocks = dp.get("blocks") or []
        # the recall re-read closure needs THIS run's block text; recall_floor calls it
        # with block_id/section/field_path only, so inject `blocks` here.
        reread = ((lambda **kw: recall_reread_fn(blocks=blocks, **kw))
                  if recall_reread_fn is not None else None)
        scorecards, binding_summary = run_verifier_suite(
            schema_loader=schema_loader, binding_verifier=binding_verifier,
            envelope=envelope, links=links, blocks=blocks,
            parser_hypothesis=state.get("parser_hypothesis") or {}, gap_tolerance=gap_tol,
            block_profiles=dp.get("block_profiles") or [],
            recall_reread_fn=reread, block_reads=block_reads,
            binding_items_out=binding_items, attribution_fn=attribution_fn,
            disabled_sections=disabled_sections,
        )
        if persistence is not None:
            try:
                safe = [{"verifier_name": s.get("verifier_name"), "passed": s.get("passed"),
                         "notes": s.get("notes", "")} for s in scorecards]
                await persistence.write_artifact(
                    doc_id=state.get("doc_id") or "unknown", kind="verification_v3",
                    content=json.dumps({"scorecards": safe, "binding_verifier": binding_summary,
                                        "cycle_budget_used": state.get("repair_budget_used", 0)},
                                       indent=2, default=str))
                # ALSO write the UI-shaped verification_v2 (links + scorecards w/ error_locs +
                # attribution metrics) — the Entity/Production/Review browsers read THIS. Without
                # it a v3-only run shows no explicit links + loses scorecard trace steps. Refs are
                # kept as loc|field_name|ref so recall_floor/attribution pin to their field.
                safe_cards = [{
                    "verifier_name": s.get("verifier_name"), "passed": s.get("passed"),
                    "notes": s.get("notes", ""),
                    "error_locs": [(e.get("loc") or e.get("field_name") or e.get("ref"))
                                   for e in (s.get("field_errors") or [])][:50],
                    "metrics": s.get("metrics"),
                } for s in scorecards]
                await persistence.write_artifact(
                    doc_id=state.get("doc_id") or "unknown", kind="verification_v2",
                    content=json.dumps({"scorecards": safe_cards, "binding_verifier": binding_summary,
                                        "links": links}, indent=2, default=str))
            except Exception:
                logger.exception("verifier_v3_node: persist failed")
        # Trace: one record per scorecard + one for the binding verifier — so the
        # field timeline shows every advisory/structural check that touched a field.
        from core.trace_recorder import extend_trace, record
        recs: list[dict[str, Any]] = []
        # plain-text mapping per verifier name (the one-sentence summary the UI renders).
        _VERIFIER_PLAIN = {
            "schema_validator": "A structural check confirmed the envelope's shape matches the expected schema.",
            "coverage_audit": "A coverage check compared what was extracted against the parser hypothesis.",
            "link_consistency": "A check made sure every cross-section link points at records that actually exist.",
            "evidence_confidence": "A check made sure each finding has at least one cited block of evidence.",
            "recall_floor": "A check looked for fields the block-role mapping says should be present but came back empty.",
            "attribution": "A check made sure each attribute really describes its owner record (not a neighbour).",
            "normalization": "A check looked for fields whose canonical form differs from what was extracted.",
            "hgvs_validity": "A check confirmed every HGVS change is structurally valid.",
        }
        for s in scorecards:
            errs = s.get("field_errors") or []
            refs = [str(e.get("loc") or e.get("field_name") or e.get("ref") or "")
                    for e in errs if isinstance(e, dict)]
            vname = str(s.get("verifier_name") or "verifier")
            passed = s.get("passed", True)
            v_plain = _VERIFIER_PLAIN.get(vname, "An automated check reviewed this and " +
                                          ("found no problem." if passed else "flagged something that needed a closer look."))
            recs.append(record(
                phase="verification", agent=vname,
                plain=v_plain,
                refs=[r for r in refs if r][:25],
                input_summary="envelope + scorecards",
                output_summary=str(s.get("notes") or ("passed" if passed else "FAILED")),
                verdict="passed" if passed else "failed",
                reasoning=str(s.get("notes") or "")))
        recs.append(record(
            phase="verification", agent="LinkBindingVerifier",
            plain="A binding check confirmed the relationship each link claims is supported by its cited evidence.",
            input_summary=f"{len(links)} link(s), {len(binding_items)} item-level verdict(s)",
            output_summary=f"refuted={binding_summary.get('refuted',0)} uncertain={binding_summary.get('uncertain',0)}",
            verdict=("passed" if not (binding_summary.get("refuted") or binding_summary.get("uncertain"))
                     else "flagged")))
        # One record PER binding item — the verifier's per-ref evidence + verdict.
        _BIND_CHECK_NAME = {"V1": "grounding", "V2": "relationship", "V3": "hallucination", "V4": "link"}
        for it in binding_items:
            chk = str(it.get("check") or "bind").upper()
            chk_name = _BIND_CHECK_NAME.get(chk, chk.lower())
            recs.append(record(
                phase="verification",
                agent=f"LinkBindingVerifier · {chk}",
                plain=f"A {chk_name} binding check ran on this item against its cited evidence.",
                refs=[str(it.get("ref") or "")],
                input_summary=f"check={it.get('check','?')}",
                output_summary=str(it.get("verdict") or "—"),
                verdict=str(it.get("verdict") or ""),
                reasoning=str(it.get("evidence") or "")))
        return {"verifier_scorecards": scorecards, "binding_verifier": binding_summary,
                "block_reads": block_reads, "binding_items": binding_items,
                "agent_trace": extend_trace(state.get("agent_trace"), *recs)}
    return verifier_node


def _make_selfcorrecting_persist_node(*, persistence: Any):
    """Commit on auto_accept OR partial_accept; always write the repair ledger +
    block_reads + escalation queue artifacts (audit + the M8 SME queue)."""
    @otel_trace("pipeline.graph_selfcorrecting.persist_node")
    async def persist_node(state: PipelineState) -> dict[str, Any]:
        try:
            run_id = await persistence.write_run(state)
        except Exception:
            logger.exception("persist_v3: write_run failed for %s", state.get("doc_id"))
            run_id = None
        extraction_id = None
        if state.get("verdict") in ("auto_accept", "partial_accept") and state.get("extraction"):
            try:
                extraction_id = await persistence.write_extraction(state, run_id=run_id)
            except Exception:
                logger.exception("persist_v3: write_extraction failed for %s", state.get("doc_id"))
            # Emit the production-schema view alongside our envelope (P3-M9). Pure/offline;
            # guarded so a mapping/convert error never fails the run or drops other artifacts.
            try:
                from transform.to_production import to_production
                production = to_production(state["extraction"])
                await persistence.write_artifact(
                    doc_id=state.get("doc_id") or "unknown", kind="extraction_production",
                    content=json.dumps(production, indent=2, default=str))
            except Exception:
                logger.exception("persist_v3: to_production emit failed for %s", state.get("doc_id"))
        # P3-M10d: per-type contested-rate metrics (the pruning feed). Pure/offline;
        # guarded so a metrics error never fails the run.
        link_metrics = None
        try:
            from pipeline.link_metrics import build_link_metrics
            link_metrics = build_link_metrics(state)
        except Exception:
            logger.exception("persist_v3: build_link_metrics failed for %s", state.get("doc_id"))
        for kind, payload in (("repair_log", state.get("repair_log")),
                              ("block_reads", state.get("block_reads")),
                              ("vmaw_log", state.get("vmaw_log")),
                              ("agent_trace", state.get("agent_trace")),
                              ("binding_items", state.get("binding_items")),
                              ("link_metrics", link_metrics),
                              ("escalation_queue", state.get("escalation_queue"))):
            if payload:
                try:
                    await persistence.write_artifact(
                        doc_id=state.get("doc_id") or "unknown", kind=kind,
                        content=json.dumps(payload, indent=2, default=str))
                except Exception:
                    logger.exception("persist_v3: failed to write %s artifact", kind)
        return {"bigquery_run_row_id": run_id, "bigquery_extraction_row_id": extraction_id}
    return persist_node


@otel_trace("pipeline.graph_selfcorrecting.build")
def build_selfcorrecting_graph(
    deps: GraphDependencies,
    *,
    triage_agent: TriageAgent | None = None,
    repair_executor: RepairExecutor | None = None,
    vmaw_agent: VMAWAgent | None = None,
    recall_reread_fn: Any | None = None,
    teams_cfg: dict[str, Any] | None = None,
    checkpointer: Any | None = None,
) -> Any:
    from langgraph.graph import END, StateGraph

    from preprocess.preprocess_node import make_preprocess_node

    if teams_cfg is None:
        import yaml
        teams_cfg = yaml.safe_load(open("config/teams.yaml", encoding="utf-8"))
    team_keys = list(deps.teams.keys())

    # recall-floor AI re-read: prefer an explicit arg, else the Gemini hook on deps
    # (getattr keeps the offline gates' NS stub deps safe).
    recall_reread_fn = recall_reread_fn or getattr(deps, "recall_reread_fn", None)
    triage_agent = triage_agent or TriageAgent(triage_llm=getattr(deps, "triage_llm", None))
    repair_executor = repair_executor or RepairExecutor(
        teams=deps.teams, normalizers=getattr(deps, "normalizers", None))
    # P3-M10c: construct VMAW with the Gemini-backed EC/CITE/VA hooks when the cloud
    # deps provide them; getattr keeps the offline gates' NS stub deps safe.
    vmaw_agent = vmaw_agent or VMAWAgent(**(getattr(deps, "vmaw_hooks", None) or {}))

    g = StateGraph(PipelineState)
    g.add_node("document_received", _document_received_v3_node)
    g.add_node("preprocess", make_preprocess_node(deps.preprocess_deps))
    g.add_node("planner", _make_planner_node(planner=deps.planner, teams_cfg=teams_cfg, team_keys=team_keys))
    g.add_node("teams", _make_teams_node(teams=deps.teams))
    g.add_node("linker", _make_linker_node(linker=deps.linker, persistence=deps.persistence))
    g.add_node("verifiers", _make_selfcorrecting_verifier_node(
        schema_loader=deps.schema_loader, binding_verifier=deps.binding_verifier,
        gap_tol=deps.coverage_gap_tolerance, recall_reread_fn=recall_reread_fn,
        persistence=deps.persistence, attribution_fn=getattr(deps, "attribution_fn", None),
        disabled_sections=getattr(deps, "disabled_sections", None)))
    g.add_node("triage", make_triage_node(agent=triage_agent))
    g.add_node("repair", make_repair_node(executor=repair_executor))
    g.add_node("vmaw", make_vmaw_node(agent=vmaw_agent))
    g.add_node("decision_router", make_decision_router_v3_node(router=deps.decision_router))
    g.add_node("persist", _make_selfcorrecting_persist_node(persistence=deps.persistence))

    g.set_entry_point("document_received")
    g.add_edge("document_received", "preprocess")
    g.add_edge("preprocess", "planner")
    g.add_edge("planner", "teams")
    g.add_edge("teams", "linker")
    g.add_edge("linker", "verifiers")
    g.add_edge("verifiers", "triage")
    # the loop: triage routes to repair (addressable + budget) or, when settled,
    # to VMAW (deep-resolve the escalations) → router.
    g.add_conditional_edges("triage", triage_route,
                            {"repair": "repair", "done": "vmaw"})
    g.add_edge("repair", "linker")          # re-link → re-verify → triage (cycle)
    g.add_edge("vmaw", "decision_router")   # deep resolution before the human
    g.add_edge("decision_router", "persist")
    g.add_edge("persist", END)

    compiled = g.compile(checkpointer=checkpointer)
    logger.info("graph_selfcorrecting: compiled (%d teams, repair loop active, recursion_limit=%d)",
                len(deps.teams), GRAPH_RECURSION_LIMIT)
    return compiled
