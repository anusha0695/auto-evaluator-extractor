"""
SchemaValidator — deterministic validation of the assembled schema-v2 envelope.

Two responsibilities:

  1. Envelope assembly (Phase 1 only). Phase 1 has just the MetadataTeam, so
     the other 3 umbrellas (`Genomic_Variant_umbrella`,
     `other_molecular_biomarker_umbrella`, `tested_biomarker_umbrella`) are
     emitted as empty placeholders to satisfy the always-emit-all-4 rule.
     Phase 2 adds the Linking agent that does proper assembly from all 4
     team outputs.

  2. Root-level validation. Uses:
       - `SchemaLoader.validate(section, ...)` per-section (Pydantic).
       - `jsonschema.Draft7Validator` against the root schema for the
         envelope-level `count_of_extracted_objects` and the per-umbrella
         `count_of_*` invariants.

Emits a `VerifierScorecard` regardless of pass/fail (the decision router
needs to see it). On pass: scorecard has `passed=True`, no `field_errors`.
On fail: `passed=False`, field-level error list, and the bad envelope is
NOT placed in state.extraction.

The team-emit-sme-flag short-circuit:
  - When `state["_team_verdict"] == "sme_flag"`, the validator still runs
    a sanity check on what little output exists and emits a scorecard, but
    sets `passed=True` with a note explaining the team flag. The decision
    router will see the team flag and route to sme_flag regardless.
"""

from __future__ import annotations

import logging
from typing import Any

from core.errors import SchemaValidationError
from core.observability import trace as otel_trace
from core.schema_loader import SchemaLoader
from core.state import PipelineState, VerifierScorecard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Envelope assembly (Phase 1)
# ---------------------------------------------------------------------------


def build_phase1_envelope(report_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Wrap the MetadataTeam's `report_metadata` payload in the full
    schema-v2 envelope with empty placeholders for the other 3 umbrellas.

    Phase 2 replaces this with the Linking agent's proper multi-team merge.
    """
    return {
        # report_metadata is not an "extracted object" (no array); the count
        # sums only the 3 array umbrellas. For Phase 1 all 3 are empty → 0.
        "count_of_extracted_objects": 0,
        "report_metadata": report_metadata or {},
        "Genomic_Variant_umbrella": {
            "count_of_Genomic_Variants": 0,
            "llm_confidence_score": None,
            "Genomic_Variants": [],
        },
        "other_molecular_biomarker_umbrella": {
            "count": 0,
            "llm_confidence_score": None,
            "other_molecular_biomarkers": [],
        },
        "tested_biomarker_umbrella": {
            "count_of_tested_biomarkers": 0,
            "page_numbers": [],
            "llm_confidence_score": None,
            "tested_biomarkers": [],
        },
    }


# ---------------------------------------------------------------------------
# SchemaValidator
# ---------------------------------------------------------------------------


class SchemaValidator:
    """Runs per-section Pydantic + root jsonschema validation."""

    def __init__(self, *, schema_loader: SchemaLoader) -> None:
        self._schema_loader = schema_loader

    @otel_trace("verification.schema_validator.validate_envelope")
    def validate_envelope(
        self,
        envelope: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Validate the full schema-v2 envelope. Returns (passed, errors)."""
        errors: list[dict[str, Any]] = []

        # ----- Per-section Pydantic validation -----
        for section in self._schema_loader.list_sections():
            payload = envelope.get(section)
            if payload is None:
                errors.append({
                    "section": section,
                    "loc": [section],
                    "msg": "umbrella section missing from envelope",
                })
                continue
            try:
                self._schema_loader.validate(section, payload)  # type: ignore[arg-type]
            except SchemaValidationError as exc:
                for e in exc.context.get("errors", []):
                    errors.append({
                        "section": section,
                        "loc": list(e.get("loc", [])),
                        "msg": e.get("msg", str(e)),
                        "type": e.get("type", ""),
                    })

        # ----- Root-level jsonschema validation -----
        try:
            from jsonschema import Draft7Validator

            root_schema = self._schema_loader.get_root_schema()
            validator = Draft7Validator(root_schema)
            for err in validator.iter_errors(envelope):
                errors.append({
                    "section": "<root>",
                    "loc": list(err.absolute_path),
                    "msg": err.message,
                    "type": getattr(err, "validator", ""),
                })
        except ImportError:
            errors.append({
                "section": "<root>",
                "loc": [],
                "msg": "jsonschema not installed — root-level validation skipped",
                "type": "ImportError",
            })

        return (not errors), errors


# ---------------------------------------------------------------------------
# LangGraph node factory
# ---------------------------------------------------------------------------


def make_schema_validator_node(*, schema_loader: SchemaLoader):
    """Return an async LangGraph node bound to the given SchemaLoader.

    Reads `state["team_outputs"]["metadata_team"]` (or honors a pre-existing
    sme_flag from the team), assembles the Phase 1 envelope, validates,
    and emits a `VerifierScorecard` into `state["verifier_scorecards"]`.

    Sets `state["extraction"]` to the validated envelope on pass; leaves it
    None on fail.
    """
    validator = SchemaValidator(schema_loader=schema_loader)

    @otel_trace("verification.schema_validator.node")
    async def schema_validator_node(state: PipelineState) -> dict[str, Any]:
        team_outputs = state.get("team_outputs") or {}
        metadata_output = team_outputs.get("metadata_team")

        existing_scorecards: list[VerifierScorecard] = list(
            state.get("verifier_scorecards") or []
        )

        # ----- Team SME-flag short-circuit -----
        if state.get("_team_verdict") == "sme_flag":
            scorecard = VerifierScorecard(
                verifier_name="schema_validator",
                passed=True,
                field_errors=[],
                notes=(
                    "Skipped detailed validation: team emitted sme_flag — "
                    f"{state.get('_team_verdict_reason')}"
                ),
            )
            return {
                "verifier_scorecards": existing_scorecards + [scorecard],
                # extraction stays None; persistence skips the extractions row
            }

        envelope = build_phase1_envelope(metadata_output)
        passed, errors = validator.validate_envelope(envelope)

        scorecard = VerifierScorecard(
            verifier_name="schema_validator",
            passed=passed,
            field_errors=errors[:25],   # cap; UI shows the rest on demand
            notes=(
                "Phase 1 envelope (report_metadata populated; other 3 umbrellas "
                "empty placeholders) validated against schema v2."
                if passed
                else f"{len(errors)} validation error(s); first: "
                     f"{errors[0]['section']}.{errors[0]['loc']}: {errors[0]['msg']}"
            ),
        )

        delta: dict[str, Any] = {
            "verifier_scorecards": existing_scorecards + [scorecard],
        }
        if passed:
            delta["extraction"] = envelope
        else:
            logger.warning(
                "schema_validator: doc_id=%s FAILED with %d error(s); first: %s",
                state.get("doc_id"), len(errors), errors[0] if errors else None,
            )
        return delta

    return schema_validator_node
