"""
SME Review Portal — Flask Server
Serves UI files and routes artifact data from EITHER a local directory OR a GCS
bucket. Same URL scheme in both modes — the browser (`ui/app.js`) is
source-agnostic and needs no changes across environments.

The set of documents is discovered at request time by scanning the configured
source for entries that contain `extraction_v2.json`. There is no hardcoded doc
list — adding a new run (e.g. `local_runs/artifacts/specimen_demo/` or
`gs://<bucket>/artifacts/specimen_demo/`) makes it appear in the portal
automatically on the next page load.

Configuration (all via environment variables):
  GCS_ARTIFACTS_BUCKET — bucket name. Set = GCS mode; unset = local mode.
  GCS_ARTIFACTS_PREFIX — key prefix inside the bucket. Default: "artifacts/".
  GOOGLE_APPLICATION_CREDENTIALS — path to service-account JSON (optional on
                                    GKE / Cloud Run with Workload Identity).
  PORT                 — server port. Default: 8501.
"""
import io
import os
import sys
from pathlib import Path

# Make this script runnable BOTH via `make portal` (which sets PYTHONPATH=.)
# AND via a bare `python ui/app.py` (which doesn't). In the bare case,
# sys.path only contains `ui/`, and `core/` sits at the repo root — a sibling
# of ui — so `import core.env_loader` would fail. Adding the repo root
# here makes the import resolve either way.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Also ensure the ui/ directory itself is importable so `from schemas import ...`
# resolves regardless of how the app is invoked (test_client, make portal, or
# `python ui/app.py`). The review layer imports flat names for module isolation.
_UI_DIR_FOR_PATH = Path(__file__).resolve().parent
if str(_UI_DIR_FOR_PATH) not in sys.path:
    sys.path.insert(0, str(_UI_DIR_FOR_PATH))

# Load `.env` into os.environ before any env-driven configuration below.
# `core.env_loader` does this on import (idempotent, no override of shell env).
# Fallback to a direct dotenv call if `core` isn't importable — keeps this
# script functional in stripped-down checkouts.
try:
    import core.env_loader  # noqa: F401  — imported for side effect (load .env)
except ImportError:
    try:
        from dotenv import load_dotenv
        _env_file = _REPO_ROOT / ".env"
        if _env_file.is_file():
            load_dotenv(_env_file, override=False)
    except ImportError:
        pass  # dotenv also missing — rely on shell env only

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

# Review layer (isolated ui/ module per NFR-1.2 of sme_capture_requirements.md).
# These imports use the flat module names (see sys.path insert above).
from field_id_generator import generate_field_ids
from review_store import EXTRACTION_FILE, get_review_store
from schemas import ActionType, ReviewAction

# Resolve paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = Path(__file__).resolve().parent

# ── Data source configuration ──────────────────────────────────────────────
# GCS is opt-in via env var. Empty / unset → local filesystem mode
# (today's behavior). Set → route reads through google-cloud-storage instead.
BUCKET_NAME = os.environ.get("GCS_ARTIFACTS_BUCKET", "").strip()
BUCKET_PREFIX = os.environ.get("GCS_ARTIFACTS_PREFIX", "artifacts/").strip()
if BUCKET_PREFIX and not BUCKET_PREFIX.endswith("/"):
    BUCKET_PREFIX += "/"
USE_GCS = bool(BUCKET_NAME)

# Local fallback — kept even in GCS mode so a stray path calculation still has
# a valid Path root (no code accidentally dereferences a None).
ARTIFACTS_DIR = PROJECT_ROOT / "local_runs" / "artifacts"

# Lazy-initialize the GCS client only when GCS mode is actually used. This
# keeps local dev friction-free — an operator without gcloud credentials can
# still boot the server as long as GCS_ARTIFACTS_BUCKET is unset.
_gcs_bucket_singleton = None


def _bucket():
    """Return the GCS Bucket, initializing the client on first use."""
    global _gcs_bucket_singleton
    if _gcs_bucket_singleton is None:
        # Import inside the function so `google.cloud.storage` is not a hard
        # runtime dependency when USE_GCS is False.
        from google.cloud import storage
        _gcs_bucket_singleton = storage.Client().bucket(BUCKET_NAME)
    return _gcs_bucket_singleton


# MIME mapping shared by both local and GCS branches.
_MIME_MAP = {
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".pb": "application/octet-stream",
}


app = Flask(__name__, static_folder=None)


# ── UI routes ──────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the main HTML page."""
    return send_from_directory(UI_DIR, "index.html")


@app.route("/styles.css")
def styles():
    return send_from_directory(UI_DIR, "styles.css")


@app.route("/<path:filename>")
def ui_static(filename):
    """Serve JS and other static UI files."""
    if (UI_DIR / filename).is_file():
        return send_from_directory(UI_DIR, filename)
    abort(404)


# ── Document discovery ────────────────────────────────────────

@app.route("/api/docs")
def list_docs():
    """
    Return the list of documents available from the configured artifact source.

    A doc counts as a document when the source has an entry for it containing
    `extraction_v2.json` (the canonical pipeline output). `label` is the doc
    id so the sidebar shows e.g. "demo" or "specimen_demo" instead of a
    hardcoded "source.pdf".

    Response shape (identical across local + GCS modes so the browser is
    unaware of the switch): `{"docs": [{"id", "label", "dir", "hasPdf"}]}`.
    """
    docs = []
    if USE_GCS:
        # Single bucket-list call — group blobs by their leading "folder"
        # (doc_id) and mark which ones have extraction_v2.json + source.pdf.
        seen_extraction: set[str] = set()
        seen_pdf: set[str] = set()
        for blob in _bucket().list_blobs(prefix=BUCKET_PREFIX):
            rel = blob.name[len(BUCKET_PREFIX):]     # e.g. "demo/extraction_v2.json"
            if "/" not in rel:
                continue
            doc_id, _, fname = rel.partition("/")
            if fname == "extraction_v2.json":
                seen_extraction.add(doc_id)
            elif fname == "source.pdf":
                seen_pdf.add(doc_id)
        for doc_id in sorted(seen_extraction):
            docs.append({
                "id": doc_id,
                "label": doc_id,
                "dir": f"/artifacts/{doc_id}/",
                "hasPdf": doc_id in seen_pdf,
            })
    else:
        # Local filesystem: scan ARTIFACTS_DIR for subfolders with extraction_v2.json.
        if ARTIFACTS_DIR.is_dir():
            for d in sorted(ARTIFACTS_DIR.iterdir()):
                if d.is_dir() and (d / "extraction_v2.json").is_file():
                    docs.append({
                        "id": d.name,
                        "label": d.name,
                        "dir": f"/artifacts/{d.name}/",
                        "hasPdf": (d / "source.pdf").is_file(),
                    })
    return jsonify({"docs": docs})


# ── Artifact data routes ───────────────────────────────────────

@app.route("/artifacts/<doc_id>/<path:filename>")
def serve_artifact(doc_id, filename):
    """
    Serve one artifact file. Routes through GCS when GCS_ARTIFACTS_BUCKET is
    set; falls back to `local_runs/artifacts/<doc_id>/` otherwise.

    The URL contract is identical in both modes (`/artifacts/<doc>/<file>`
    returns raw bytes with the right MIME), so the browser (`ui/app.js`)
    fetches the same way regardless of where the bytes come from.
    """
    # Universal path-traversal defense — reject anything suspicious before
    # touching either backend.
    if "/" in doc_id or ".." in doc_id or ".." in filename:
        abort(400, description="Invalid path")

    mimetype = _MIME_MAP.get(
        os.path.splitext(filename)[1].lower(),
        "application/octet-stream",
    )

    if USE_GCS:
        blob = _bucket().blob(f"{BUCKET_PREFIX}{doc_id}/{filename}")
        if not blob.exists():
            abort(404, description=f"Artifact not found: {doc_id}/{filename}")
        # Stream blob bytes through Flask. For very large source.pdf you may
        # want to switch to a signed-URL redirect to avoid buffering the whole
        # file in the Flask process's memory (see docs/architecture/components.md
        # §9 for the pattern).
        return send_file(
            io.BytesIO(blob.download_as_bytes()),
            mimetype=mimetype,
            download_name=filename,
        )

    # Local filesystem branch (unchanged behavior).
    safe_doc_dir = (ARTIFACTS_DIR / doc_id).resolve()
    if not safe_doc_dir.is_dir() or ARTIFACTS_DIR.resolve() not in safe_doc_dir.parents:
        abort(404, description=f"Unknown document: {doc_id}")

    filepath = safe_doc_dir / filename
    if not filepath.is_file():
        abort(404, description=f"Artifact not found: {doc_id}/{filename}")

    return send_file(filepath, mimetype=mimetype)


# ══════════════════════════════════════════════════════════════
# ── SME Review capture endpoints (DR-4 in sme_capture_requirements.md) ──
# ══════════════════════════════════════════════════════════════
#
# 7 endpoints under /api/review/* backed by ui/review_store.py.
# All persistence + merge logic lives in the review_store; these routes
# are thin adapters that validate input, call the store, and JSONify the
# result. Same env vars (GCS_ARTIFACTS_BUCKET) select the storage backend
# — no separate configuration.

# Lazy singleton — matches the _bucket() pattern above. The store's factory
# reads env vars at construction time; deferring gives the caller a chance
# to set env before the first request lands.
_review_store_singleton = None


def _review_store():
    """Return the ReviewStore, initializing on first call."""
    global _review_store_singleton
    if _review_store_singleton is None:
        _review_store_singleton = get_review_store()
    return _review_store_singleton


def _validate_doc_id(doc_id: str) -> None:
    """Path-traversal defense — mirror the rule used in /artifacts/."""
    if "/" in doc_id or ".." in doc_id or not doc_id:
        abort(400, description=f"invalid doc_id: {doc_id!r}")


def _validate_action_id(action_id: str) -> None:
    if "/" in action_id or ".." in action_id or not action_id:
        abort(400, description=f"invalid action_id: {action_id!r}")


# ── GET /api/review/<doc_id>/field-ids ────────────────────────

@app.route("/api/review/<doc_id>/field-ids")
def get_review_field_ids(doc_id: str):
    """
    Return the list of stable field IDs for a doc (FR-1).
    Generated deterministically from extraction_v2.json.
    """
    _validate_doc_id(doc_id)
    store = _review_store()
    extraction = store.backend.read_json(doc_id, EXTRACTION_FILE)
    if extraction is None:
        abort(404, description=f"extraction not found for doc: {doc_id}")
    ids = generate_field_ids(extraction)
    return jsonify({"field_ids": [fi.to_dict() for fi in ids]})


# ── GET /api/review/<doc_id>/state ────────────────────────────

@app.route("/api/review/<doc_id>/state")
def get_review_state(doc_id: str):
    """
    Return current review state + computed metrics + progress summary.
    Used by the UI on doc open to restore prior actions (FR-3.4, FR-5.1).
    """
    _validate_doc_id(doc_id)
    store = _review_store()
    state = store.load_state(doc_id)
    metrics = store.compute_doc_metrics(doc_id)

    # submission_status of "draft" but zero actions → "not_started" surface state
    surface_status = state.submission_status
    if state.submission_status == "draft" and not state.actions:
        surface_status = "not_started"

    return jsonify({
        "state": state.to_dict(),
        "metrics": metrics.to_dict(),
        "progress": {
            "fields_reviewed": metrics.fields_reviewed,
            "fields_total": metrics.fields_total,
            "submission_status": surface_status,
        },
    })


# ── POST /api/review/<doc_id>/action ──────────────────────────

@app.route("/api/review/<doc_id>/action", methods=["POST"])
def post_review_action(doc_id: str):
    """
    Autosave one SME action (FR-3.1).

    Body:
      {
        "field_id": "Genomic_Variants[0].amino_acid_change",
        "action":   "correct",
        "original_value": "p.V600E",
        "corrected_value": "p.Val600Glu",
        "natural_key": null   // optional
      }
    """
    _validate_doc_id(doc_id)
    payload = request.get_json(silent=True) or {}

    action_str = payload.get("action")
    try:
        action_type = ActionType(action_str)
    except (ValueError, TypeError):
        abort(400, description=f"invalid action type: {action_str!r}")

    field_id = payload.get("field_id")
    if not field_id or not isinstance(field_id, str):
        abort(400, description="field_id is required")

    action = ReviewAction(
        action_id="",       # auto-assigned by the store
        field_id=field_id,
        action=action_type,
        timestamp="",       # auto-assigned by the store
        natural_key=payload.get("natural_key"),
        original_value=payload.get("original_value"),
        corrected_value=payload.get("corrected_value"),
    )
    saved = _review_store().save_action(doc_id, action)
    return jsonify({
        "action_id": saved.action_id,
        "timestamp": saved.timestamp,
        "saved": True,
    })


# ── POST /api/review/<doc_id>/submit ──────────────────────────

@app.route("/api/review/<doc_id>/submit", methods=["POST"])
def post_review_submit(doc_id: str):
    """
    Commit the review (FR-4 / FR-8).

    Body: {"force": false, "reviewer_id": "alice"}

    Response is SubmitResult.to_dict() — either:
      {"status": "submitted", "ground_truth_path": "..."}
    or (when force=false and untouched fields exist):
      {"status": "confirm_required", "unreviewed_count": N, "unreviewed_fields": [...]}
    """
    _validate_doc_id(doc_id)
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force", False))
    reviewer_id = payload.get("reviewer_id")
    try:
        result = _review_store().submit(doc_id, force=force, reviewer_id=reviewer_id)
    except FileNotFoundError as e:
        abort(404, description=str(e))
    return jsonify(result.to_dict())


# ── POST /api/review/<doc_id>/reopen ──────────────────────────

@app.route("/api/review/<doc_id>/reopen", methods=["POST"])
def post_review_reopen(doc_id: str):
    """
    Flip a submitted doc back to draft (FR-5.2). No-op if not submitted.
    ground_truth.json remains on disk until next submit overwrites.
    """
    _validate_doc_id(doc_id)
    _review_store().reopen(doc_id)
    return jsonify({"submission_status": "draft"})


# ── GET /api/review/metrics ───────────────────────────────────

@app.route("/api/review/metrics")
def get_review_metrics_aggregate():
    """
    Landing-page dashboard payload (FR-7.1, FR-7.2).

    Returns:
      {
        "aggregate": {...Metrics.to_dict()...},
        "per_doc":  [
          {doc_id, submission_status, metrics|null, fields_reviewed, fields_total, submitted_at},
          ...
        ]
      }
    """
    aggregate, per_doc = _review_store().compute_all_metrics()
    return jsonify({"aggregate": aggregate.to_dict(), "per_doc": per_doc})


# ── DELETE /api/review/<doc_id>/action/<action_id> ────────────

@app.route("/api/review/<doc_id>/action/<action_id>", methods=["DELETE"])
def delete_review_action(doc_id: str, action_id: str):
    """
    Undo one action — marks it as superseded per DR-2.2.
    The row is retained (append-only); the effective state ignores it.

    Returns 404 if the action doesn't exist or is already superseded.
    """
    _validate_doc_id(doc_id)
    _validate_action_id(action_id)
    superseded_at = _review_store().supersede_action(doc_id, action_id)
    if superseded_at is None:
        abort(404, description=f"action not found or already superseded: {action_id}")
    return jsonify({
        "deleted_action_id": action_id,
        "superseded_at": superseded_at,
    })


# ── Run ────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8501))
    print(f"  Serving UI from:        {UI_DIR}")
    if USE_GCS:
        print(f"  Serving artifacts from: gs://{BUCKET_NAME}/{BUCKET_PREFIX}")
    else:
        print(f"  Serving artifacts from: {ARTIFACTS_DIR}  (local mode — "
              f"set GCS_ARTIFACTS_BUCKET to switch)")
    print(f"  Open http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
