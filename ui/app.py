"""
SME Review Portal — Flask Server
Serves UI files and routes artifact data directly from local_runs/artifacts/<doc_id>/.
No data is copied; artifacts are served from their original location.

The set of documents is discovered at request time by scanning ARTIFACTS_DIR for
sub-folders that contain `extraction_v2.json`. There is no hardcoded doc list —
adding a new run (e.g. `local_runs/artifacts/specimen_demo/`) makes it appear in
the portal automatically on the next page load.

Port can be overridden via the `PORT` env var (default 8501).
"""
import os
from pathlib import Path
from flask import Flask, send_from_directory, send_file, abort, jsonify

# Resolve paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = Path(__file__).resolve().parent
# Parent of all per-document artifact folders. Each subdir is one document.
ARTIFACTS_DIR = PROJECT_ROOT / "local_runs" / "artifacts"

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
    Return the list of documents available in ARTIFACTS_DIR.

    A folder counts as a document when it contains `extraction_v2.json`
    (the canonical pipeline output). `label` is the folder name so the
    sidebar shows e.g. "demo" or "specimen_demo" instead of a hardcoded
    "source.pdf".
    """
    docs = []
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
    Serve artifact files directly from local_runs/artifacts/<doc_id>/.
    No duplication — reads from the original pipeline output location.
    """
    # Block path traversal: doc_id must be a single existing subdir name.
    safe_doc_dir = (ARTIFACTS_DIR / doc_id).resolve()
    if not safe_doc_dir.is_dir() or ARTIFACTS_DIR.resolve() not in safe_doc_dir.parents:
        abort(404, description=f"Unknown document: {doc_id}")

    filepath = safe_doc_dir / filename
    if not filepath.is_file():
        abort(404, description=f"Artifact not found: {doc_id}/{filename}")

    # Determine MIME type
    ext = filepath.suffix.lower()
    mime_map = {
        ".json": "application/json",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".pb": "application/octet-stream",
    }
    mimetype = mime_map.get(ext, "application/octet-stream")
    return send_file(filepath, mimetype=mimetype)


# ── Run ────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8501))
    print(f"  Serving UI from:        {UI_DIR}")
    print(f"  Serving artifacts from: {ARTIFACTS_DIR}")
    print(f"  Open http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
