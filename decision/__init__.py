"""
extractor.decision — verdict router.

Reads verifier scorecards + team verdict + LLM confidence; emits one of:

    auto_accept   — commits to BigQuery extractions table + GCS artifacts.
    sme_flag      — writes the run row with verdict=sme_flag (extraction
                    skipped), surfaces in the Phase 1 Streamlit UI for SME
                    review. Phase 4 wires the human-in-the-loop interrupt
                    + correction loop.

Phase 1 has only `schema_validator` as a verifier and a single team, so the
routing logic is short. Phase 2 adds 3 more verifiers and 3 more teams;
this module's interface stays the same.
"""

from decision.decision_router import (
    DecisionRouter,
    make_decision_router_node,
)

__all__ = [
    "DecisionRouter",
    "make_decision_router_node",
]
