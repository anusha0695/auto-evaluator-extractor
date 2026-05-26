"""
runner — the pipeline's single entry point.

Boots dependencies for the requested pipeline version, compiles the graph,
invokes it with a seed state, returns a `RunResult` summarizing the outcome.

Public API:

    await run(doc_id, gcs_uri, version="v1")  → RunResult

Used by `scripts/process_local.py` for dev runs and (Phase 4) by
`scripts/deploy_dataflow.py` for the production Dataflow job. Same code
path; the only difference is who calls it.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from core.errors import PipelineError
from core.observability import ObservabilityManager, trace as otel_trace
from core.state import PipelineState

logger = logging.getLogger(__name__)


PipelineVersion = Literal["v1", "v2", "v3", "v4"]


@dataclass
class RunResult:
    """Summary returned to the caller (CLI / Dataflow / tests)."""

    doc_id: str
    gcs_uri: str
    pipeline_version: PipelineVersion
    verdict: Literal["auto_accept", "fixable", "sme_flag", "errored"] | None = None
    verdict_reason: str = ""
    extraction: dict[str, Any] | None = None
    artifacts_gcs_prefix: str | None = None
    bigquery_run_row_id: str | None = None
    bigquery_extraction_row_id: str | None = None
    otel_trace_id: str | None = None
    total_latency_ms: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    # Full final state for tests / debug. Not surfaced in the UI.
    final_state: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@otel_trace("pipeline.runner.run")
async def run(
    doc_id: str,
    gcs_uri: str | None = None,
    *,
    local_pdf_path: str | None = None,
    version: PipelineVersion = "v1",
    deps: Any | None = None,
    graph: Any | None = None,
) -> RunResult:
    """Run the pipeline end-to-end on one document.

    Args:
        doc_id: short identifier for the doc — used in GCS paths, BigQuery
            rows, and OTel span tags.
        gcs_uri: gs://patient_clinical_trial/patient_profiles/<doc>.pdf.
            Mutually exclusive with `local_pdf_path`.
        local_pdf_path: path to a local PDF file. The bytes are read and
            sent inline to DocAI's RawDocument API — no GCS upload of the
            input needed. Output artifacts still go to GCS regardless.
            Useful for fast dev iteration on prompt tuning.
        version: pipeline version. Phase 1 supports only "v1".
        deps: pre-built `GraphV1Dependencies` (for tests).
        graph: pre-compiled graph (for tests).

    Returns:
        RunResult.
    """
    ObservabilityManager.init_from_env()

    if version not in ("v1", "v2", "v3"):
        raise PipelineError(
            f"runner supports version='v1' (Phase 1), 'v2' (Phase 2a), and 'v3' "
            f"(Phase 3 repair loop + VMAW); got {version!r}. v4 ships in Phase 4.",
            doc_id=doc_id,
            retry_safe=False,
        )
    if bool(gcs_uri) == bool(local_pdf_path):
        raise PipelineError(
            "runner.run: provide EXACTLY one of gcs_uri / local_pdf_path",
            doc_id=doc_id,
            retry_safe=False,
        )

    t0 = time.monotonic()
    result = RunResult(
        doc_id=doc_id,
        gcs_uri=gcs_uri or f"local://{local_pdf_path}",
        pipeline_version=version,
    )

    # ----- Build dependencies + graph (dispatch by version) -----
    if version == "v3":
        # graph_selfcorrecting reuses the v2 dependency set (teams/linker/binding/router) and
        # adds the triage→repair loop + VMAW on the escalate branch. VMAW LLM hooks
        # default off → it degrades to "escalate to SME", but the escalation_queue /
        # agent_trace / repair_log / vmaw_log artifacts the SME UI needs are produced.
        if deps is None:
            from pipeline.graph_linear import build_graph_dependencies
            deps = build_graph_dependencies()
        if graph is None:
            from core.checkpointer import build_checkpointer
            from pipeline.graph_selfcorrecting import build_selfcorrecting_graph
            graph = build_selfcorrecting_graph(deps, checkpointer=build_checkpointer(deps.storage_config))
    elif version == "v2":
        if deps is None:
            from pipeline.graph_linear import build_graph_dependencies
            deps = build_graph_dependencies()
        if graph is None:
            from pipeline.graph_linear import build_linear_graph
            from core.checkpointer import build_checkpointer
            graph = build_linear_graph(deps, checkpointer=build_checkpointer(deps.storage_config))
    else:
        if deps is None:
            from pipeline.graph_v1 import build_graph_v1_dependencies
            deps = build_graph_v1_dependencies()
        if graph is None:
            from pipeline.graph_v1 import build_graph_v1
            graph = build_graph_v1(deps)

    # ----- Build the seed state -----
    raw_pdf_bytes: bytes | None = None
    if local_pdf_path:
        from pathlib import Path
        p = Path(local_pdf_path).expanduser()
        if not p.is_absolute():
            p = p.resolve()
        if not p.exists():
            # Try common fallback locations relative to the repo root.
            cwd = Path.cwd()
            repo_root = Path(__file__).resolve().parent.parent
            tried = [str(p)]
            candidates = [
                repo_root.parent / "data" / "actual_docs" / p.name,
                repo_root / "data" / "actual_docs" / p.name,
                cwd / "data" / "actual_docs" / p.name,
            ]
            found = None
            for c in candidates:
                c = c.resolve()
                tried.append(str(c))
                if c.exists():
                    found = c
                    break
            if found is None:
                raise PipelineError(
                    f"Local PDF not found.\n"
                    f"  Original input:  {local_pdf_path!r}\n"
                    f"  Resolved to:     {p}\n"
                    f"  Cwd:             {cwd}\n"
                    f"  Tried fallbacks: {tried[1:]}\n"
                    f"  Hint: pass an absolute path or `../data/actual_docs/<file>.pdf` "
                    f"from inside the extractor/ directory.",
                    doc_id=doc_id,
                    retry_safe=False,
                )
            logger.info("runner: PDF resolved via fallback to %s", found)
            p = found
        raw_pdf_bytes = p.read_bytes()
        logger.info("runner: read %d bytes from local PDF %s", len(raw_pdf_bytes), p)

    seed: dict[str, Any] = {
        "doc_id": doc_id,
        "gcs_uri": gcs_uri,             # None when running on local PDF
        "raw_pdf_bytes": raw_pdf_bytes, # None when running on GCS PDF
        "pipeline_version": version,
        "team_outputs": {},
        "verifier_scorecards": [],
        "preprocessing_errors": [],
        "latency_ms": 0,
        "cost_usd": 0.0,
    }

    # ----- Invoke the graph -----
    thread_id = f"{doc_id}-{uuid.uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread_id}}
    if version == "v3":
        # hard backstop for the triage→repair loop (recur-guard + budget terminate
        # well before this; it only guards bugs).
        from pipeline.graph_selfcorrecting import GRAPH_RECURSION_LIMIT
        cfg["recursion_limit"] = GRAPH_RECURSION_LIMIT

    try:
        final = await graph.ainvoke(seed, config=cfg)
    except Exception as exc:
        result.verdict = "errored"
        result.error = f"{type(exc).__name__}: {exc}"
        result.total_latency_ms = int((time.monotonic() - t0) * 1000)
        logger.exception(
            "runner: doc_id=%s ERRORED after %dms — %s",
            doc_id, result.total_latency_ms, exc,
        )
        return result

    # ----- Pull fields out of the final state -----
    result.verdict = final.get("verdict")
    result.verdict_reason = final.get("verdict_reason", "")
    result.extraction = final.get("extraction")
    result.artifacts_gcs_prefix = final.get("artifacts_gcs_prefix")
    result.bigquery_run_row_id = final.get("bigquery_run_row_id")
    result.bigquery_extraction_row_id = final.get("bigquery_extraction_row_id")
    result.otel_trace_id = final.get("otel_trace_id")
    result.total_latency_ms = int(final.get("latency_ms", 0)) or int(
        (time.monotonic() - t0) * 1000
    )
    result.cost_usd = float(final.get("cost_usd", 0.0))
    result.final_state = dict(final)

    logger.info(
        "runner: doc_id=%s verdict=%s in %dms — %s",
        doc_id, result.verdict, result.total_latency_ms, result.verdict_reason[:120],
    )
    return result
