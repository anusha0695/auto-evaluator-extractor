"""
extractor.verification — deterministic, LLM-free checks on team output.

Phase 1 ships ONE verifier:

    SchemaValidator   — runs the schema-v2 Pydantic + jsonschema validators
                        on the assembled schema envelope.

Phase 2 adds three more (coverage_audit, link_consistency,
evidence_confidence). The verifier interface is intentionally small —
every verifier consumes state, emits a `VerifierScorecard`.
"""

from verification.schema_validator import (
    SchemaValidator,
    build_phase1_envelope,
    make_schema_validator_node,
)

__all__ = [
    "SchemaValidator",
    "make_schema_validator_node",
    "build_phase1_envelope",
]
