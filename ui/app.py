"""
SME Review Portal — Flask Server
Serves UI files and routes artifact data directly from local_runs/artifacts/demo/
No data is copied; artifacts are served from their original location.
"""
import os
from pathlib import Path
from flask import Flask, send_from_directory, send_file, abort

# Resolve paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = PROJECT_ROOT / "local_runs" / "artifacts" / "demo"

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


# ── Artifact data routes ───────────────────────────────────────

@app.route("/artifacts/<path:filename>")
def serve_artifact(filename):
    """
    Serve artifact files directly from local_runs/artifacts/demo/.
    No duplication — reads from the original pipeline output location.
    """
    filepath = ARTIFACTS_DIR / filename
    if not filepath.is_file():
        abort(404, description=f"Artifact not found: {filename}")

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
    print(f"  Serving UI from:       {UI_DIR}")
    print(f"  Serving artifacts from: {ARTIFACTS_DIR}")
    print(f"  Open http://localhost:8501")
    app.run(host="0.0.0.0", port=8501, debug=True)
