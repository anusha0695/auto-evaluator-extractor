"""
SME Review Portal — data schemas.

Pure-stdlib dataclasses + one Enum defining every persisted shape:
FieldIdentifier, ReviewAction, ReviewState, Metrics, SubmitResult.

All dataclasses provide `to_dict()` / `from_dict()` for lossless JSON
round-tripping — the review-store layer uses these when reading/writing
`review_actions.json`.

Design constraints:
  - Pure stdlib only (NFR-1.2 in sme_capture_requirements.md): no imports
    from core/, pipeline/, teams/, preprocess/, agents/, or any other
    application module. Only `dataclasses`, `enum`, `typing`.
  - No I/O. This file is data shapes only.
  - Field names match DR-2.1 (schema) EXACTLY so review_actions.json is
    a direct serialization.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Union


__all__ = [
    "ActionType",
    "FieldIdentifier",
    "ReviewAction",
    "ReviewState",
    "Metrics",
    "SubmitResult",
]


# ─── Enums ────────────────────────────────────────────────────────────────

class ActionType(str, Enum):
    """
    The five SME review actions per FR-2 plus one system-generated type.

    ACCEPT/CORRECT/REJECT operate at the field level.
    REJECT_RECORD operates at the record level (removes a whole array entry).
    ADD_MISSING adds a record the extractor missed.
    IMPLICIT_ACCEPT is generated at submit time (FR-8) for fields the SME
      didn't touch — distinguished from explicit ACCEPT so we can measure
      review thoroughness later.
    """
    ACCEPT = "accept"
    CORRECT = "correct"
    REJECT = "reject"
    REJECT_RECORD = "reject_record"
    ADD_MISSING = "add_missing"
    IMPLICIT_ACCEPT = "implicit_accept"

    def __str__(self) -> str:  # so f-strings render "accept" not "ActionType.ACCEPT"
        return self.value


# ─── Field identity ───────────────────────────────────────────────────────

@dataclass
class FieldIdentifier:
    """
    A stable, deterministic identifier for one reviewable field per FR-1.

    Attributes:
        id: dot-notation ID per FR-1.1
            examples:
              "report_metadata.patient_name"
              "Genomic_Variant_umbrella[3].amino_acid_change"
              "Genomic_Variant_umbrella[3].variant_details.hgvs_c"
        natural_key: optional {key_field: value} for records with stable IDs
            (e.g. {"variant_id": "v_abc"}); enables re-mapping if extraction
            re-runs change array ordering (FR-1.4). None for singular fields
            or when no natural key exists.
        path_in_extraction: sequence of keys/indices to walk from extraction
            root to reach the field's value. Enables the review store to
            look up the original value without re-parsing the field_id.
    """
    id: str
    natural_key: Optional[dict[str, Any]] = None
    path_in_extraction: list[Union[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "natural_key": self.natural_key,
            "path_in_extraction": list(self.path_in_extraction),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FieldIdentifier":
        return cls(
            id=d["id"],
            natural_key=d.get("natural_key"),
            path_in_extraction=list(d.get("path_in_extraction", [])),
        )


# ─── Review action (one row inside actions[]) ────────────────────────────

@dataclass
class ReviewAction:
    """
    One SME action on one field or record per DR-2.1.

    `original_value` and `corrected_value` are Any because ADD_MISSING
    carries a full record dict as its corrected_value; field-level actions
    carry strings or None.

    Supersession: when SME changes their mind about a field, the prior
    action is RETAINED with `superseded_at` and `superseded_by` populated
    (per DR-2.2). Metrics/merge code only considers the LATEST un-superseded
    action per field_id.
    """
    action_id: str
    field_id: str
    action: ActionType
    timestamp: str                            # ISO 8601 UTC
    natural_key: Optional[dict[str, Any]] = None
    original_value: Any = None
    corrected_value: Any = None
    implicit: bool = False                    # True only for FR-8 warn-then-accept
    superseded_at: Optional[str] = None
    superseded_by: Optional[str] = None       # action_id of the replacement

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "field_id": self.field_id,
            "natural_key": self.natural_key,
            "action": self.action.value,
            "original_value": self.original_value,
            "corrected_value": self.corrected_value,
            "timestamp": self.timestamp,
            "implicit": self.implicit,
            "superseded_at": self.superseded_at,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReviewAction":
        return cls(
            action_id=d["action_id"],
            field_id=d["field_id"],
            action=ActionType(d["action"]),
            timestamp=d["timestamp"],
            natural_key=d.get("natural_key"),
            original_value=d.get("original_value"),
            corrected_value=d.get("corrected_value"),
            implicit=d.get("implicit", False),
            superseded_at=d.get("superseded_at"),
            superseded_by=d.get("superseded_by"),
        )

    def is_active(self) -> bool:
        """True if this action has not been superseded — i.e. counts in metrics."""
        return self.superseded_at is None


# ─── Review state (top-level review_actions.json shape) ──────────────────

@dataclass
class ReviewState:
    """
    Top-level container for review_actions.json per DR-2.1.

    submission_status:
      "draft"     — SME is still working; ground_truth.json does not exist
      "submitted" — SME clicked Submit; ground_truth.json has been written

    submission_history: prior submissions (list of dicts with
      submitted_at, submitted_by, actions_snapshot_hash) — populated on
      re-submits per FR-5.3.
    """
    doc_id: str
    extraction_source: str
    submission_status: str                    # "draft" | "submitted"
    created_at: str                           # ISO 8601 UTC
    last_action_at: str                       # ISO 8601 UTC
    submitted_at: Optional[str] = None        # None while draft
    submitted_by: Optional[str] = None        # from SME_REVIEWER_ID env / UI input
    submission_history: list[dict[str, Any]] = field(default_factory=list)
    actions: list[ReviewAction] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "extraction_source": self.extraction_source,
            "submission_status": self.submission_status,
            "created_at": self.created_at,
            "last_action_at": self.last_action_at,
            "submitted_at": self.submitted_at,
            "submitted_by": self.submitted_by,
            "submission_history": list(self.submission_history),
            "actions": [a.to_dict() for a in self.actions],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReviewState":
        return cls(
            doc_id=d["doc_id"],
            extraction_source=d["extraction_source"],
            submission_status=d["submission_status"],
            created_at=d["created_at"],
            last_action_at=d["last_action_at"],
            submitted_at=d.get("submitted_at"),
            submitted_by=d.get("submitted_by"),
            submission_history=list(d.get("submission_history", [])),
            actions=[ReviewAction.from_dict(a) for a in d.get("actions", [])],
        )

    def active_actions(self) -> list[ReviewAction]:
        """Return only the actions that haven't been superseded."""
        return [a for a in self.actions if a.is_active()]

    def latest_action_per_field(self) -> dict[str, ReviewAction]:
        """
        Map field_id → the latest un-superseded action for that field.

        Used by metrics computation and by the submit merge logic to
        determine each field's effective final state (DR-2.3).
        """
        return {a.field_id: a for a in self.active_actions()}


# ─── Metrics ──────────────────────────────────────────────────────────────

@dataclass
class Metrics:
    """
    Per-doc or aggregate metrics per FR-6 (per-doc) and FR-7.1 (aggregate).

    Ratios (accuracy, precision, recall) are computed from the raw counters
    via the formulas in FR-6.1/6.2/6.3.

    Aggregate metrics are SUMS of per-doc counters with ratios recomputed
    on the summed totals — NEVER means of per-doc ratios. This is required
    by FR-7.1 to avoid biasing small docs vs large ones.

    ratios are stored as 0.0-1.0 floats; the UI multiplies by 100 for display.
    """
    accept_count: int = 0
    correct_count: int = 0
    reject_count: int = 0                     # field-level rejects
    reject_record_count: int = 0              # record-level rejects
    add_missing_count: int = 0
    implicit_accept_count: int = 0
    fields_reviewed: int = 0                  # explicit touches (accept+correct+reject+add_missing+reject_record)
    fields_total: int = 0                     # total reviewable fields in extraction_v2.json
    accuracy: float = 0.0
    precision: float = 0.0
    recall: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "accept_count": self.accept_count,
            "correct_count": self.correct_count,
            "reject_count": self.reject_count,
            "reject_record_count": self.reject_record_count,
            "add_missing_count": self.add_missing_count,
            "implicit_accept_count": self.implicit_accept_count,
            "fields_reviewed": self.fields_reviewed,
            "fields_total": self.fields_total,
            "accuracy": round(self.accuracy, 4),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Metrics":
        return cls(
            accept_count=int(d.get("accept_count", 0)),
            correct_count=int(d.get("correct_count", 0)),
            reject_count=int(d.get("reject_count", 0)),
            reject_record_count=int(d.get("reject_record_count", 0)),
            add_missing_count=int(d.get("add_missing_count", 0)),
            implicit_accept_count=int(d.get("implicit_accept_count", 0)),
            fields_reviewed=int(d.get("fields_reviewed", 0)),
            fields_total=int(d.get("fields_total", 0)),
            accuracy=float(d.get("accuracy", 0.0)),
            precision=float(d.get("precision", 0.0)),
            recall=float(d.get("recall", 0.0)),
        )


# ─── Submit result ────────────────────────────────────────────────────────

@dataclass
class SubmitResult:
    """
    Response shape from `POST /api/review/<doc_id>/submit` per FR-8 / DR-4.

    status:
      "submitted"         — commit succeeded, ground_truth.json written
      "confirm_required"  — untouched fields exist; client re-POSTs with
                            force=true to accept them implicitly (FR-8.2)
    """
    status: str                               # "submitted" | "confirm_required"
    unreviewed_count: int = 0
    unreviewed_fields: list[str] = field(default_factory=list)
    ground_truth_path: Optional[str] = None   # populated on "submitted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "unreviewed_count": self.unreviewed_count,
            "unreviewed_fields": list(self.unreviewed_fields),
            "ground_truth_path": self.ground_truth_path,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SubmitResult":
        return cls(
            status=d["status"],
            unreviewed_count=int(d.get("unreviewed_count", 0)),
            unreviewed_fields=list(d.get("unreviewed_fields", [])),
            ground_truth_path=d.get("ground_truth_path"),
        )
