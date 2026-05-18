"""
graph_v1 — frozen Phase 1 LangGraph composition.

Linear flow (no conditional edges in Phase 1):

    document_received ──► preprocess ──► metadata_team ──► schema_validator
                                                                  │
                                                                  ▼
                                                          decision_router
                                                                  │
                                                                  ▼
                                                              persist ──► END

`document_received` is a tiny entry-point node that validates the seed
state has `doc_id` + `gcs_uri` + `pipeline_version` set.

The graph is **frozen** after Phase 1 ships. Phase 2 ships `graph_v2.py`
with the Planner + 4 teams + Linking + full verifier suite as a separate
file. `pipeline/runner.py` dispatches between the two by `--version`.

Dependency injection: every node closes over the dependencies it needs
(persistence, schema loader, etc.). `build_graph_v1_dependencies()` is
the one-shot factory the runner calls.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from agents import Arbiter, CoverageAuditor, Extractor
from core.checkpointer import build_checkpointer
from core.errors import PipelineError
from core.observability import trace as otel_trace
from core.persistence import Persistence, StorageConfig, load_storage_config
from core.prompt_renderer import PromptRenderer
from core.schema_loader import SchemaLoader
from core.state import PipelineState
from decision.decision_router import DecisionRouter, make_decision_router_node
from preprocess.block_profiler import BlockProfiler
from preprocess.docai_parser import DocAIParser
from preprocess.fax_header_filter import FaxHeaderFilter
from preprocess.medical_ner import SciSpaCyMedicalNER
from preprocess.preprocess_node import (
    PreprocessNodeDependencies,
    make_preprocess_node,
)
from teams import MetadataTeam
from verification.schema_validator import make_schema_validator_node

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------


@dataclass
class GraphV1Dependencies:
    """All the components graph_v1 needs. Built once per runner boot."""

    storage_config: StorageConfig
    schema_loader: SchemaLoader
    prompt_renderer: PromptRenderer
    persistence: Persistence
    preprocess_deps: PreprocessNodeDependencies
    metadata_team: MetadataTeam
    auto_accept_confidence_threshold: float


def build_graph_v1_dependencies(
    *,
    storage_config: StorageConfig | None = None,
    schema_path: str = "config/schemas/genomic_pathology_v2.json",
    prompts_root: str = "config/prompts",
    tools_yaml_path: str = "config/tools.yaml",
    teams_yaml_path: str = "config/teams.yaml",
) -> GraphV1Dependencies:
    """One-shot factory. Reads config, builds every component."""
    import yaml

    cfg = storage_config or load_storage_config("phase_1")
    schema_loader = SchemaLoader.from_path(schema_path)
    prompt_renderer = PromptRenderer(schema_loader=schema_loader, prompts_root=prompts_root)
    persistence = Persistence(cfg)

    # Preprocess layer
    docai = DocAIParser(persistence=persistence)
    fax_filter = FaxHeaderFilter()
    block_profiler = BlockProfiler(prompt_renderer=prompt_renderer)
    medical_ner = SciSpaCyMedicalNER(prompt_renderer=prompt_renderer)
    preprocess_deps = PreprocessNodeDependencies(
        docai_parser=docai,
        fax_filter=fax_filter,
        block_profiler=block_profiler,
        medical_ner=medical_ner,
        persistence=persistence,
    )

    # MetadataTeam (read teams.yaml for tool allowlist + threshold)
    teams_cfg = yaml.safe_load(open(teams_yaml_path, encoding="utf-8"))
    mt_cfg = teams_cfg["teams"]["metadata_team"]

    extractor = Extractor(
        team_name="MetadataTeam",
        schema_section="report_metadata",
        team_prompt_template=mt_cfg["prompt_template"],
        tool_allowlist=list(mt_cfg["tool_allowlist"]),
        prompt_renderer=prompt_renderer,
        schema_loader=schema_loader,
        pipeline_version="v1",
        tools_yaml_path=tools_yaml_path,
        model_name=mt_cfg["models"]["extractor"]["name"],
        temperature=float(mt_cfg["models"]["extractor"]["temperature"]),
    )
    auditor = CoverageAuditor(
        team_name="MetadataTeam",
        schema_section="report_metadata",
        prompt_renderer=prompt_renderer,
        coverage_gap_tolerance=float(mt_cfg.get("coverage_gap_tolerance", 0.10)),
        model_name=mt_cfg["models"]["coverage_auditor"]["name"],
        temperature=float(mt_cfg["models"]["coverage_auditor"]["temperature"]),
    )
    arbiter = Arbiter(
        team_name="MetadataTeam",
        schema_section="report_metadata",
        prompt_renderer=prompt_renderer,
        pipeline_version="v1",
        vmaw_available=False,   # Phase 1 — INVOKE_VMAW maps to sme_flag
        model_name=mt_cfg["models"]["arbiter"]["name"],
        temperature=float(mt_cfg["models"]["arbiter"]["temperature"]),
    )
    metadata_team = MetadataTeam(
        extractor=extractor,
        coverage_auditor=auditor,
        arbiter=arbiter,
        vmaw_available=False,
    )

    return GraphV1Dependencies(
        storage_config=cfg,
        schema_loader=schema_loader,
        prompt_renderer=prompt_renderer,
        persistence=persistence,
        preprocess_deps=preprocess_deps,
        metadata_team=metadata_team,
        auto_accept_confidence_threshold=float(
            mt_cfg.get("auto_accept_confidence_threshold", 0.85)
        ),
    )


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


@otel_trace("pipeline.graph_v1.document_received")
async def _document_received_node(state: PipelineState) -> dict[str, Any]:
    """Entry node — sanity-checks the seed state."""
    missing = [k for k in ("doc_id", "pipeline_version") if not state.get(k)]
    if not state.get("gcs_uri") and not state.get("raw_pdf_bytes"):
        missing.append("gcs_uri or raw_pdf_bytes")
    if missing:
        raise PipelineError(
            f"document_received: required state field(s) missing: {missing}",
            doc_id=state.get("doc_id"),
            retry_safe=False,
            context={"missing": missing},
        )
    if state.get("pipeline_version") != "v1":
        raise PipelineError(
            f"document_received: graph_v1 invoked with "
            f"pipeline_version={state.get('pipeline_version')!r}; expected 'v1'",
            doc_id=state.get("doc_id"),
            retry_safe=False,
        )
    return {
        "team_outputs": {},
        "verifier_scorecards": [],
        "preprocessing_errors": [],
        "latency_ms": 0,
        "cost_usd": 0.0,
    }


def _make_metadata_team_node(*, metadata_team: MetadataTeam):
    @otel_trace("pipeline.graph_v1.metadata_team_node")
    async def metadata_team_node(state: PipelineState) -> dict[str, Any]:
        team_result = await metadata_team.run(state)
        team_outputs = dict(state.get("team_outputs") or {})
        if team_result.output is not None:
            team_outputs["metadata_team"] = team_result.output

        delta: dict[str, Any] = {
            "team_outputs": team_outputs,
            "_team_verdict": team_result.verdict,
            "_team_verdict_reason": team_result.verdict_reason,
            "latency_ms": int(state.get("latency_ms", 0)) + team_result.total_latency_ms,
        }
        # Surface tokens as a coarse cost proxy. Real $-cost requires
        # Vertex's per-model pricing; Phase 1 just tracks tokens.
        delta["cost_usd"] = float(state.get("cost_usd", 0.0))   # unchanged until pricing wired
        return delta

    return metadata_team_node


def _make_persist_node(*, persistence: Persistence):
    @otel_trace("pipeline.graph_v1.persist_node")
    async def persist_node(state: PipelineState) -> dict[str, Any]:
        # Write the runs_v1 row (always).
        try:
            run_id = await persistence.write_run(state)
        except Exception as exc:
            logger.exception(
                "persist_node: write_run failed for doc_id=%s — continuing without "
                "BigQuery row (Phase 1 doesn't block on persistence errors).",
                state.get("doc_id"),
            )
            run_id = None

        # Write extractions_v1 only when verdict=auto_accept AND extraction is present.
        extraction_id: str | None = None
        if state.get("verdict") == "auto_accept" and state.get("extraction"):
            try:
                extraction_id = await persistence.write_extraction(state, run_id=run_id)
            except Exception:
                logger.exception(
                    "persist_node: write_extraction failed for doc_id=%s — leaving "
                    "extraction_id None.",
                    state.get("doc_id"),
                )

        return {
            "bigquery_run_row_id": run_id,
            "bigquery_extraction_row_id": extraction_id,
        }

    return persist_node


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


@otel_trace("pipeline.graph_v1.build")
def build_graph_v1(
    deps: GraphV1Dependencies,
    *,
    checkpointer: Any | None = None,
) -> Any:
    """Compile the Phase 1 LangGraph. Returns a runnable graph."""
    from langgraph.graph import END, StateGraph

    preprocess_node = make_preprocess_node(deps.preprocess_deps)
    metadata_team_node = _make_metadata_team_node(metadata_team=deps.metadata_team)
    schema_validator_node = make_schema_validator_node(schema_loader=deps.schema_loader)
    decision_router_node = make_decision_router_node(
        router=DecisionRouter(
            auto_accept_confidence_threshold=deps.auto_accept_confidence_threshold,
        )
    )
    persist_node = _make_persist_node(persistence=deps.persistence)

    # Use the real PipelineState TypedDict so LangGraph 1.x's state-merge
    # semantics preserve fields not explicitly returned by each node (with
    # plain `dict` it treats each node's return as the full replacement
    # state, dropping the seed fields).
    g = StateGraph(PipelineState)
    g.add_node("document_received", _document_received_node)
    g.add_node("preprocess", preprocess_node)
    g.add_node("metadata_team", metadata_team_node)
    g.add_node("schema_validator", schema_validator_node)
    g.add_node("decision_router", decision_router_node)
    g.add_node("persist", persist_node)

    g.set_entry_point("document_received")
    g.add_edge("document_received", "preprocess")
    g.add_edge("preprocess", "metadata_team")
    g.add_edge("metadata_team", "schema_validator")
    g.add_edge("schema_validator", "decision_router")
    g.add_edge("decision_router", "persist")
    g.add_edge("persist", END)

    checkpointer = checkpointer or build_checkpointer(deps.storage_config)
    compiled = g.compile(checkpointer=checkpointer)
    logger.info("graph_v1: compiled with checkpointer=%s", type(checkpointer).__name__)
    return compiled
