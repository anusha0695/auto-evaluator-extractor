"""
SME Review Portal — persistence layer.

Reads/writes `review_actions.json` and `ground_truth.json` under either a
local filesystem OR a GCS bucket, chosen at runtime by the same env vars
that select the artifact source (`GCS_ARTIFACTS_BUCKET`). Same URL layout
in both modes — see DR-3 in sme_capture_requirements.md.

Architecture:

  StorageBackend (abstract)
      ├── LocalBackend   — filesystem under local_runs/artifacts/<doc>/
      └── GCSBackend     — google-cloud-storage under gs://<bucket>/<prefix><doc>/

  ReviewStore(backend)
      ├── load_state(doc_id)              → ReviewState
      ├── save_action(doc_id, action)     → ReviewAction (autosave one)
      ├── submit(doc_id, force, reviewer) → SubmitResult (FR-4/FR-8 flow)
      ├── reopen(doc_id)                  → None
      ├── compute_doc_metrics(doc_id)     → Metrics
      └── compute_all_metrics()           → (aggregate: Metrics, per_doc: list[dict])

  get_review_store()  — factory that chooses backend from env

Design constraints (from sme_capture_requirements.md):
  * NFR-1.2: no imports from core/, pipeline/, teams/, etc. Only stdlib
    plus google.cloud.storage (imported lazily inside GCSBackend only).
  * NFR-5.4: atomic writes on both backends (LocalBackend: temp+rename;
    GCSBackend: uploads are atomic per-blob).
  * NFR-6.1: extraction_v2.json is NEVER modified. It is read-only input.

Threading/concurrency: v1 assumes single-writer per doc (last-writer-wins
across concurrent SMEs). Multi-writer conflict resolution is parked as T5.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import tempfile
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from field_id_generator import generate_field_ids
from metrics import aggregate_metrics, compute_per_doc_metrics
from schemas import (
    ActionType,
    FieldIdentifier,
    Metrics,
    ReviewAction,
    ReviewState,
    SubmitResult,
)


# ─── Filenames (public constants) ────────────────────────────────────────

EXTRACTION_FILE = "extraction_v2.json"
GROUND_TRUTH_FILE = "ground_truth.json"
REVIEW_ACTIONS_FILE = "review_actions.json"


# ═════════════════════════════════════════════════════════════════════════
# Storage backends
# ═════════════════════════════════════════════════════════════════════════


class StorageBackend(ABC):
    """
    Abstract read/write of JSON blobs at (doc_id, filename).

    Both LocalBackend and GCSBackend implement this interface with identical
    contract: writes are atomic, reads return None for missing files, no
    exceptions on absent keys.
    """

    @abstractmethod
    def read_json(self, doc_id: str, filename: str) -> Optional[dict[str, Any]]:
        """Return the parsed JSON, or None if the file/blob does not exist."""

    @abstractmethod
    def write_json_atomic(self, doc_id: str, filename: str, data: dict[str, Any]) -> None:
        """Atomically replace or create the file/blob with the JSON contents."""

    @abstractmethod
    def exists(self, doc_id: str, filename: str) -> bool:
        """Return whether the file/blob currently exists."""

    @abstractmethod
    def list_doc_ids(self) -> list[str]:
        """Return doc_ids that have `extraction_v2.json` (same rule as /api/docs)."""

    @abstractmethod
    def describe(self) -> str:
        """Human-readable backend description for logging."""


class LocalBackend(StorageBackend):
    """Filesystem backend rooted at local_runs/artifacts/."""

    def __init__(self, root_dir: Path | str):
        self.root = Path(root_dir)

    def _path(self, doc_id: str, filename: str) -> Path:
        # Path-traversal defense — same rule as ui/app.py.
        if "/" in doc_id or ".." in doc_id or ".." in filename:
            raise ValueError(f"invalid doc_id or filename: {doc_id!r}, {filename!r}")
        return self.root / doc_id / filename

    def read_json(self, doc_id: str, filename: str) -> Optional[dict[str, Any]]:
        p = self._path(doc_id, filename)
        if not p.is_file():
            return None
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)

    def write_json_atomic(self, doc_id: str, filename: str, data: dict[str, Any]) -> None:
        target = self._path(doc_id, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to temp in the SAME DIRECTORY so os.replace is a rename (atomic on POSIX
        # and Windows). Cross-filesystem temp locations would break atomicity.
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{filename}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, target)   # atomic on POSIX + Windows
        except Exception:
            # Best-effort cleanup on failure — don't leave orphan .tmp files
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def exists(self, doc_id: str, filename: str) -> bool:
        return self._path(doc_id, filename).is_file()

    def list_doc_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            d.name for d in self.root.iterdir()
            if d.is_dir() and (d / EXTRACTION_FILE).is_file()
        )

    def describe(self) -> str:
        return f"LocalBackend({self.root})"


class GCSBackend(StorageBackend):
    """
    google-cloud-storage backend rooted at gs://<bucket>/<prefix>.

    Lazy client init — imports google.cloud.storage on first bucket() call
    so pure-local runs don't pay the import cost.

    GCS atomicity: single blob uploads are atomic — a partially-uploaded
    blob is never visible to readers. That's sufficient for FR-3.2 per
    file. Cross-file atomicity (ground_truth + review_actions together) is
    handled by ReviewStore.submit()'s ordering: ground_truth first, then
    review_actions. If review_actions fails after ground_truth succeeded,
    reopening the doc will show submission_status="draft" but ground_truth
    exists — reconcilable, not corrupt.
    """

    def __init__(self, bucket_name: str, prefix: str = "artifacts/"):
        self.bucket_name = bucket_name
        self.prefix = prefix if prefix.endswith("/") else prefix + "/"
        self._bucket = None  # lazy

    def _get_bucket(self):
        if self._bucket is None:
            from google.cloud import storage   # lazy import
            self._bucket = storage.Client().bucket(self.bucket_name)
        return self._bucket

    def _blob_name(self, doc_id: str, filename: str) -> str:
        if "/" in doc_id or ".." in doc_id or ".." in filename:
            raise ValueError(f"invalid doc_id or filename: {doc_id!r}, {filename!r}")
        return f"{self.prefix}{doc_id}/{filename}"

    def read_json(self, doc_id: str, filename: str) -> Optional[dict[str, Any]]:
        blob = self._get_bucket().blob(self._blob_name(doc_id, filename))
        if not blob.exists():
            return None
        return json.loads(blob.download_as_bytes().decode("utf-8"))

    def write_json_atomic(self, doc_id: str, filename: str, data: dict[str, Any]) -> None:
        blob = self._get_bucket().blob(self._blob_name(doc_id, filename))
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        # upload_from_file is atomic per the GCS API — no partial-visible state
        blob.upload_from_file(io.BytesIO(payload), content_type="application/json")

    def exists(self, doc_id: str, filename: str) -> bool:
        return self._get_bucket().blob(self._blob_name(doc_id, filename)).exists()

    def list_doc_ids(self) -> list[str]:
        docs_with_extraction: set[str] = set()
        for blob in self._get_bucket().list_blobs(prefix=self.prefix):
            rel = blob.name[len(self.prefix):]
            if "/" not in rel:
                continue
            doc_id, _, fname = rel.partition("/")
            if fname == EXTRACTION_FILE:
                docs_with_extraction.add(doc_id)
        return sorted(docs_with_extraction)

    def describe(self) -> str:
        return f"GCSBackend(gs://{self.bucket_name}/{self.prefix})"


# ═════════════════════════════════════════════════════════════════════════
# ReviewStore — the public API
# ═════════════════════════════════════════════════════════════════════════


class ReviewStore:
    """
    High-level review persistence, storage-backend-agnostic.

    All methods are safe to call whether the doc has never been reviewed,
    has a draft, or has been submitted.
    """

    def __init__(self, backend: StorageBackend):
        self.backend = backend

    # ── State loading ─────────────────────────────────────────────────

    def load_state(self, doc_id: str) -> ReviewState:
        """
        Return the current review state for a doc.

        If review_actions.json exists → load it.
        Otherwise return a fresh empty state with submission_status="draft".
        """
        existing = self.backend.read_json(doc_id, REVIEW_ACTIONS_FILE)
        if existing is not None:
            return ReviewState.from_dict(existing)

        now = _now_iso()
        return ReviewState(
            doc_id=doc_id,
            extraction_source=EXTRACTION_FILE,
            submission_status="draft",
            created_at=now,
            last_action_at=now,
        )

    # ── Save one action (autosave) ────────────────────────────────────

    def save_action(self, doc_id: str, action: ReviewAction) -> ReviewAction:
        """
        Append `action` to review_actions.json.

        If a prior un-superseded action for the same field_id exists, it is
        marked as superseded (DR-2.2/2.3): the prior row is retained with
        superseded_at + superseded_by populated.

        Returns the persisted action (with a server-assigned action_id if
        the input's was empty).
        """
        state = self.load_state(doc_id)

        # Auto-assign action_id if not provided
        if not action.action_id:
            action.action_id = f"act-{uuid.uuid4().hex[:12]}"

        # Ensure timestamp is set
        if not action.timestamp:
            action.timestamp = _now_iso()

        # Supersede any prior un-superseded action for the same field_id
        for prior in state.actions:
            if prior.field_id == action.field_id and prior.is_active():
                prior.superseded_at = action.timestamp
                prior.superseded_by = action.action_id

        state.actions.append(action)
        state.last_action_at = action.timestamp

        self.backend.write_json_atomic(doc_id, REVIEW_ACTIONS_FILE, state.to_dict())
        return action

    # ── Submit ────────────────────────────────────────────────────────

    def submit(
        self,
        doc_id: str,
        force: bool = False,
        reviewer_id: Optional[str] = None,
    ) -> SubmitResult:
        """
        Commit the review per FR-4 / FR-8.

        Flow:
          1. Load state + extraction_v2.json.
          2. Generate all field IDs from extraction.
          3. Find untouched fields (not in latest_action_per_field).
          4. If untouched > 0 and NOT force → return SubmitResult(
                 status="confirm_required", unreviewed_count, unreviewed_fields
             ).
          5. Otherwise, generate implicit_accept actions for every untouched
             field (with implicit=True per DR-2.4).
          6. Build ground_truth.json by applying all latest actions to a
             deep copy of extraction (FR-4.3).
          7. Insert _review_metadata block into ground_truth (DR-1.2).
          8. Update state — submission_status="submitted", submitted_at/by,
             append prior submission to submission_history if resubmitting.
          9. Write ground_truth.json FIRST, then review_actions.json.
             (Order: if step 9b fails after 9a, next load sees a mismatch
             — state=draft but ground_truth exists — reconcilable, not
             corrupt.)
        """
        state = self.load_state(doc_id)
        extraction = self.backend.read_json(doc_id, EXTRACTION_FILE)
        if extraction is None:
            raise FileNotFoundError(f"{EXTRACTION_FILE} not found for doc {doc_id!r}")

        field_ids = generate_field_ids(extraction)
        latest = state.latest_action_per_field()
        touched = set(latest.keys())

        # Fields inside a rejected record should NOT get implicit_accepts —
        # the SME has said the whole record shouldn't exist. Collect those
        # record prefixes (e.g. "Genomic_Variants[7]") and filter them out.
        rejected_record_prefixes = tuple(
            f"{fid}."
            for fid, act in latest.items()
            if act.action == ActionType.REJECT_RECORD
        )

        untouched_ids = [
            fi.id
            for fi in field_ids
            if fi.id not in touched
            and not fi.id.startswith(rejected_record_prefixes)
        ]

        if untouched_ids and not force:
            # User feedback: send ALL unreviewed field IDs so the UI can render
            # a scrollable list with click-to-jump. Even for large docs (~3k
            # fields) this is only a few hundred KB of JSON strings.
            return SubmitResult(
                status="confirm_required",
                unreviewed_count=len(untouched_ids),
                unreviewed_fields=untouched_ids,
            )

        now = _now_iso()

        # Generate implicit_accept actions for untouched fields per FR-8.3
        for uf_id in untouched_ids:
            state.actions.append(
                ReviewAction(
                    action_id=f"act-{uuid.uuid4().hex[:12]}",
                    field_id=uf_id,
                    action=ActionType.IMPLICIT_ACCEPT,
                    timestamp=now,
                    implicit=True,
                )
            )

        # If this doc has been submitted before, archive the prior submission
        # metadata. Use `submitted_at is not None` (not `submission_status ==
        # "submitted"`) because reopen() flips status to "draft" while leaving
        # submitted_at as a marker that a prior submission exists.
        if state.submitted_at is not None:
            state.submission_history.append({
                "submitted_at": state.submitted_at,
                "submitted_by": state.submitted_by,
                "actions_snapshot_hash": _sha256_of_dict(state.to_dict()),
            })

        state.submission_status = "submitted"
        state.submitted_at = now
        state.submitted_by = reviewer_id or _default_reviewer_id()
        state.last_action_at = now

        # Build ground truth
        field_ids_by_id = {fi.id: fi for fi in field_ids}
        ground_truth = self._build_ground_truth(
            extraction, state, field_ids_by_id, all_field_ids_count=len(field_ids)
        )

        # Write ground_truth FIRST — if the second write fails, we still have
        # the committed ground truth. Reopening will notice the mismatch.
        self.backend.write_json_atomic(doc_id, GROUND_TRUTH_FILE, ground_truth)
        self.backend.write_json_atomic(doc_id, REVIEW_ACTIONS_FILE, state.to_dict())

        return SubmitResult(
            status="submitted",
            ground_truth_path=f"{doc_id}/{GROUND_TRUTH_FILE}",
        )

    # ── Supersede one action (DELETE endpoint per DR-4) ──────────────

    def supersede_action(self, doc_id: str, action_id: str) -> Optional[str]:
        """
        Mark one action as superseded per DR-2.2.

        Returns the supersession timestamp if the action was found and was
        active; None if the action does not exist or was already superseded.
        The action row is retained (append-only audit trail).
        """
        state = self.load_state(doc_id)
        now = _now_iso()
        for action in state.actions:
            if action.action_id == action_id and action.is_active():
                action.superseded_at = now
                action.superseded_by = f"revert-{uuid.uuid4().hex[:8]}"
                state.last_action_at = now
                self.backend.write_json_atomic(
                    doc_id, REVIEW_ACTIONS_FILE, state.to_dict()
                )
                return now
        return None

    # ── Reopen ────────────────────────────────────────────────────────

    def reopen(self, doc_id: str) -> None:
        """
        Flip a submitted doc back to draft per FR-5.2.

        The ground_truth.json stays on disk until the next submit overwrites
        it — this preserves the last-known-good ground truth in case the
        SME's re-edit is abandoned.
        """
        state = self.load_state(doc_id)
        if state.submission_status != "submitted":
            return  # no-op — nothing to reopen
        state.submission_status = "draft"
        state.last_action_at = _now_iso()
        self.backend.write_json_atomic(doc_id, REVIEW_ACTIONS_FILE, state.to_dict())

    # ── Metrics ───────────────────────────────────────────────────────

    def compute_doc_metrics(self, doc_id: str) -> Metrics:
        """Per-doc metrics per FR-6."""
        state = self.load_state(doc_id)
        extraction = self.backend.read_json(doc_id, EXTRACTION_FILE)
        fields_total = len(generate_field_ids(extraction or {}))
        return compute_per_doc_metrics(state, fields_total)

    def compute_all_metrics(self) -> tuple[Metrics, list[dict[str, Any]]]:
        """
        Aggregate metrics across all submitted docs + per-doc summaries for
        the landing page per FR-7.

        Returns:
            (aggregate_metrics_of_submitted_docs, per_doc_summaries)
            where per_doc_summaries is a list of dicts, one per doc, with:
              doc_id, submission_status, metrics_dict_or_none,
              fields_reviewed, fields_total, submitted_at
        """
        per_doc_summaries: list[dict[str, Any]] = []
        submitted_metrics: list[Metrics] = []

        for doc_id in self.backend.list_doc_ids():
            state = self.load_state(doc_id)
            extraction = self.backend.read_json(doc_id, EXTRACTION_FILE)
            fields_total = len(generate_field_ids(extraction or {}))
            m = compute_per_doc_metrics(state, fields_total)

            per_doc_summaries.append({
                "doc_id": doc_id,
                "submission_status": state.submission_status if state.actions else "not_started",
                "metrics": m.to_dict() if state.submission_status == "submitted" else None,
                "fields_reviewed": m.fields_reviewed,
                "fields_total": fields_total,
                "submitted_at": state.submitted_at,
            })

            if state.submission_status == "submitted":
                submitted_metrics.append(m)

        return aggregate_metrics(submitted_metrics), per_doc_summaries

    # ── Ground-truth build (private) ──────────────────────────────────

    def _build_ground_truth(
        self,
        extraction: dict[str, Any],
        state: ReviewState,
        field_ids_by_id: dict[str, FieldIdentifier],
        all_field_ids_count: int,
    ) -> dict[str, Any]:
        """
        Apply the merged actions to a deep copy of extraction per FR-4.3.

        Order of operations:
          1. Field-level value changes (correct → replace, reject → null) using
             ORIGINAL indices — safe because no structural change yet.
          2. Record-level rejects — remove array elements in DESCENDING index
             order per section so earlier indices remain valid during removal.
          3. Additions — append with `_added_by_sme: True` marker.
          4. Insert `_review_metadata` block per DR-1.2.
        """
        gt = copy.deepcopy(extraction)

        latest = state.latest_action_per_field()

        # Collect actions by category
        value_changes: list[tuple[list, Any]] = []
        record_rejects_by_section: dict[tuple[str, str], list[int]] = {}
        additions: list[tuple[str, str, Any]] = []

        for field_id, action in latest.items():
            atype = action.action

            if atype in (ActionType.ACCEPT, ActionType.IMPLICIT_ACCEPT):
                continue  # no change to ground truth

            if atype == ActionType.CORRECT:
                fi = field_ids_by_id.get(field_id)
                if fi is not None:
                    value_changes.append((fi.path_in_extraction, action.corrected_value))
                continue

            if atype == ActionType.REJECT:
                fi = field_ids_by_id.get(field_id)
                if fi is not None:
                    value_changes.append((fi.path_in_extraction, None))
                continue

            if atype == ActionType.REJECT_RECORD:
                # field_id = "Genomic_Variants[7]"
                parsed = _parse_record_ref(field_id)
                if parsed is None:
                    continue
                list_key, idx = parsed
                umbrella = _find_umbrella_for_list(extraction, list_key)
                if umbrella is None:
                    continue
                record_rejects_by_section.setdefault((umbrella, list_key), []).append(idx)
                continue

            if atype == ActionType.ADD_MISSING:
                # field_id = "Genomic_Variants[__new_0__]"
                list_key = field_id.split("[")[0]
                umbrella = _find_umbrella_for_list(extraction, list_key)
                if umbrella is None:
                    continue
                additions.append((umbrella, list_key, action.corrected_value))
                continue

        # 1. Apply value changes
        for path, value in value_changes:
            _set_at_path(gt, path, value)

        # 2. Apply record rejects (descending index per section — avoid shift bugs)
        for (umbrella, list_key), indices in record_rejects_by_section.items():
            for idx in sorted(indices, reverse=True):
                try:
                    del gt[umbrella][list_key][idx]
                except (KeyError, IndexError):
                    pass  # silent skip — record already gone

        # 3. Apply additions
        for umbrella, list_key, new_record in additions:
            if isinstance(new_record, dict):
                marked = {**new_record, "_added_by_sme": True}
            else:
                # scalar record — no marker slot; just append
                marked = new_record
            gt.setdefault(umbrella, {}).setdefault(list_key, []).append(marked)

        # 4. Insert _review_metadata block
        metrics = compute_per_doc_metrics(state, all_field_ids_count)
        actions_count = {t.value: 0 for t in ActionType}
        for action in state.active_actions():
            actions_count[action.action.value] += 1

        gt["_review_metadata"] = {
            "source_extraction_file": EXTRACTION_FILE,
            "source_extraction_hash": _sha256_of_dict(extraction),
            "submitted_at": state.submitted_at,
            "submitted_by": state.submitted_by,
            "submission_count": len(state.submission_history) + 1,
            "review_actions_file": REVIEW_ACTIONS_FILE,
            "actions_count": actions_count,
            "metrics": {
                "accuracy": round(metrics.accuracy, 4),
                "precision": round(metrics.precision, 4),
                "recall": round(metrics.recall, 4),
            },
        }

        return gt


# ═════════════════════════════════════════════════════════════════════════
# Factory + helpers
# ═════════════════════════════════════════════════════════════════════════


def get_review_store() -> ReviewStore:
    """
    Return a ReviewStore backed by GCS if `GCS_ARTIFACTS_BUCKET` is set, else
    a LocalBackend rooted at local_runs/artifacts/.

    Same env vars used by ui/app.py — no new configuration surface (NFR-2.2).
    """
    bucket = os.environ.get("GCS_ARTIFACTS_BUCKET", "").strip()
    if bucket:
        prefix = os.environ.get("GCS_ARTIFACTS_PREFIX", "artifacts/").strip()
        return ReviewStore(GCSBackend(bucket, prefix))

    # Local mode — resolve local_runs/artifacts/ relative to repo root
    repo_root = Path(__file__).resolve().parent.parent
    return ReviewStore(LocalBackend(repo_root / "local_runs" / "artifacts"))


# ── Small helpers ─────────────────────────────────────────────────────────


def _now_iso() -> str:
    """ISO 8601 UTC timestamp — always Z-suffixed for consistency."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_of_dict(d: dict[str, Any]) -> str:
    """Deterministic hash of a JSON-serializable dict (sorted keys)."""
    return "sha256:" + hashlib.sha256(
        json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


_RECORD_REF_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]$")


def _parse_record_ref(field_id: str) -> Optional[tuple[str, int]]:
    """Parse 'Genomic_Variants[7]' → ('Genomic_Variants', 7). None if malformed."""
    m = _RECORD_REF_RE.match(field_id)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _find_umbrella_for_list(
    extraction: dict[str, Any], list_key: str
) -> Optional[str]:
    """
    Return the top-level key whose dict-value contains a list named list_key.
    e.g. 'Genomic_Variants' → 'Genomic_Variant_umbrella'.
    """
    for top_key, top_val in extraction.items():
        if isinstance(top_val, dict) and isinstance(top_val.get(list_key), list):
            return top_key
    return None


def _set_at_path(obj: Any, path: list, value: Any) -> None:
    """Set obj[path[0]][path[1]]... = value. Silent no-op on invalid paths."""
    if not path:
        return
    for step in path[:-1]:
        try:
            obj = obj[step]
        except (KeyError, IndexError, TypeError):
            return
    try:
        obj[path[-1]] = value
    except (KeyError, IndexError, TypeError):
        return


def _default_reviewer_id() -> str:
    """SME identity for v1 — env var placeholder per requirements §8 T2."""
    return os.environ.get("SME_REVIEWER_ID", "unknown").strip() or "unknown"
