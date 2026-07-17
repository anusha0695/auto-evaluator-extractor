# ═══════════════════════════════════════════════════════════════════════
# SME Review Portal — isolated container image (NFR-1.3)
# ═══════════════════════════════════════════════════════════════════════
#
# The ui/ module ships as a self-contained image with no dependency on
# core/, pipeline/, teams/, preprocess/, or agents/ (per NFR-1.2).
#
# Build:
#   docker build -t sme-portal ui/
#
# Run — local artifact mode (mount a directory that holds
# extraction_v2.json per doc under local_runs/artifacts/<doc>/):
#   docker run --rm -p 8501:8501 \
#              -v $PWD/local_runs:/app/local_runs \
#              -e SME_REVIEWER_ID=alice \
#              sme-portal
#
# Run — GCS artifact mode (portal reads/writes artifacts to a bucket):
#   docker run --rm -p 8501:8501 \
#              -e GCS_ARTIFACTS_BUCKET=my-bucket \
#              -e GCS_ARTIFACTS_PREFIX=artifacts/ \
#              -e GOOGLE_APPLICATION_CREDENTIALS=/app/sa.json \
#              -v $PWD/sa.json:/app/sa.json:ro \
#              -e SME_REVIEWER_ID=alice \
#              sme-portal
#
# Open the portal at http://localhost:8501
# ═══════════════════════════════════════════════════════════════════════

FROM python:3.12-slim

# ─── Python-runtime settings ───────────────────────────────────────────
#   PYTHONDONTWRITEBYTECODE — no .pyc files inside the image (smaller image)
#   PYTHONUNBUFFERED        — flush stdout/stderr immediately (Docker-friendly)
#   PIP_*                    — no pip version-check noise, no pip cache
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ─── App-config env vars (overridable via `docker run -e VAR=val`) ─────
#
# Declared with defaults so `docker inspect sme-portal` reveals every knob.
# The app reads these via os.environ at startup + per-request:
#
#   PORT                    — Flask server port (mapped by -p on docker run).
#                             Default 8501.
#
#   GCS_ARTIFACTS_BUCKET    — GCS bucket name. UNSET (empty) = local mode
#                             (reads/writes to /app/local_runs/artifacts/).
#                             SET = GCS mode (reads/writes to gs://<bucket>/<prefix>).
#
#   GCS_ARTIFACTS_PREFIX    — key prefix under the bucket. Default "artifacts/".
#                             Only used when GCS_ARTIFACTS_BUCKET is set.
#
#   SME_REVIEWER_ID         — stamped into submitted_by on every submit
#                             per FR-8.5. Default "unknown". Override per-
#                             container OR override per-request from the UI.
#
#   GOOGLE_APPLICATION_CREDENTIALS — path to a mounted service-account JSON
#                             for GCS auth. UNSET is fine on GKE/Cloud Run
#                             (workload identity handles auth automatically);
#                             for local Docker runs against GCS you'd mount
#                             the key file and set this to /app/sa.json.
ENV PORT=8501 \
    GCS_ARTIFACTS_BUCKET="" \
    GCS_ARTIFACTS_PREFIX="artifacts/" \
    SME_REVIEWER_ID="unknown"

WORKDIR /app

# Copy requirements first for Docker layer caching — rebuilds only re-install
# when requirements.txt changes.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the entire ui/ module. .dockerignore excludes tests/, __pycache__,
# and other dev-only files.
COPY . /app/

# Non-root user for security (per common container hardening advice)
RUN useradd --create-home --uid 1000 portal \
 && chown -R portal:portal /app
USER portal

EXPOSE 8501

# app.py runs directly — its sys.path.insert calls handle the "run without
# PYTHONPATH=." case, so no separate entrypoint script needed.
CMD ["python", "app.py"]
