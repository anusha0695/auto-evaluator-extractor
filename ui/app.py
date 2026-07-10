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
from pathlib import Path

# Load `.env` into os.environ before any env-driven configuration below.
# `core.env_loader` does this on import (idempotent, no override of shell env).
import core.env_loader  # noqa: F401  — must be BEFORE the env reads below

from flask import Flask, abort, jsonify, send_file, send_from_directory

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
