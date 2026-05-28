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

import copy
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
# Envelope-validation schema relaxation
# ---------------------------------------------------------------------------


def _relax_schema_for_envelope(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of `schema` with every object subschema made
    nullable and tolerant of extra keys.

    This mirrors, at the jsonschema layer, the tolerance the Pydantic loader
    already applies (`extra="ignore"` + Optional nested models). Without it the
    Draft7Validator would structural-fail on exactly the payloads the section
    teams committed — a nested object emitted as `null` (e.g. an empty
    `lymph_node_details`) or carrying a plausible-but-unmodelled key (e.g.
    `lymphovascular_invasion`, `page_number`). Genuine type mismatches on a
    POPULATED object and `required`-key gaps still surface.

    Only a copy is relaxed; the strict schema returned by
    `SchemaLoader.get_root_schema()` (used as Gemini `response_schema`) is left
    untouched so structured-output generation stays strict.
    """
    return _relax_node(copy.deepcopy(schema))


def _relax_node(node: Any) -> Any:
    if isinstance(node, dict):
        t = node.get("type")
        is_object = t == "object" or (isinstance(t, list) and "object" in t)
        if is_object:
            # allow null alongside object
            if isinstance(t, str):
                node["type"] = [t, "null"]
            elif isinstance(t, list) and "null" not in t:
                node["type"] = t + ["null"]
            # tolerate extra keys
            if node.get("additionalProperties") is False:
                node["additionalProperties"] = True
        for key, val in list(node.items()):
            # recurse into schema-bearing positions only
            if key in {"properties", "patternProperties", "$defs", "definitions"} and isinstance(val, dict):
                for k2, v2 in val.items():
                    val[k2] = _relax_node(v2)
            elif key in {"items", "additionalProperties", "contains"} and isinstance(val, dict):
                node[key] = _relax_node(val)
            elif key in {"allOf", "anyOf", "oneOf"} and isinstance(val, list):
                node[key] = [_relax_node(v) for v in val]
    return node


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
        *,
        disabled_sections: set[str] | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Validate the full schema-v2 envelope. Returns (passed, errors).

        V4-M7 (D3): `disabled_sections` (e.g. significant_findings, clinical_information
        in v4) are skipped in the per-section presence check — a deliberately-off section
        is legitimately absent, not a structural error. Default None → every section is
        required (v2/v3 unchanged)."""
        errors: list[dict[str, Any]] = []
        disabled = disabled_sections or set()

        # ----- Per-section Pydantic validation -----
        for section in self._schema_loader.list_sections():
            if section in disabled:
                continue  # disabled team → section legitimately absent
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

            # Validate against a RELAXED copy that mirrors the Pydantic loader's
            # tolerance: every object subschema is made nullable and allowed to
            # carry extra keys. The teams already commit such payloads (a
            # specimen with no lymph_node_details emits null; an extractor may
            # add a plausible-but-unmodelled key like lymphovascular_invasion),
            # so the envelope verifier must not structural-fail on the very
            # shapes Pydantic accepted. Genuine type/required violations on
            # POPULATED objects still surface. We relax a deep copy only — the
            # strict schema Gemini uses as `response_schema` is untouched.
            root_schema = _relax_schema_for_envelope(self._schema_loader.get_root_schema())
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
