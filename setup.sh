#!/usr/bin/env bash
# extractor — one-shot Mac setup script.
#
# Auto-detects Python (prefers 3.11; falls back to 3.12 / 3.10; installs 3.11
# via Homebrew if none present), ensures poppler is installed (for pdf2image),
# creates a .venv, installs Phase 1 deps, then runs verify_environment.py.
#
# Re-runnable — safe to invoke multiple times. Each step is idempotent.
#
# Usage:
#   cd "/Users/maverick/Documents/Anusha/Unstructured/agentic_evaluation/extractor"
#   bash setup.sh 2>&1 | tee setup.log

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# ----------------------------- pretty printing -----------------------------
b_yel() { printf "\n\033[1;33m== %s\033[0m\n" "$*"; }
b_grn() { printf "\033[1;32m✓ %s\033[0m\n" "$*"; }
b_red() { printf "\033[1;31m✗ %s\033[0m\n" "$*"; }
b_inf() { printf "  %s\n" "$*"; }

failures=0
note_fail() { failures=$((failures + 1)); b_red "$@"; }

# ----------------------------- 1. Homebrew prereq --------------------------
b_yel "Step 1 — Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  note_fail "Homebrew not installed."
  b_inf "Install it from https://brew.sh then re-run: bash setup.sh"
  exit 1
fi
b_grn "Homebrew present: $(brew --version | head -1)"

# ----------------------------- 2. poppler ---------------------------------
b_yel "Step 2 — poppler (for pdf2image, used by the Streamlit PDF viewer)"
if brew list poppler >/dev/null 2>&1; then
  b_grn "poppler already installed."
else
  b_inf "Installing poppler — this can take ~1 minute..."
  brew install poppler || { note_fail "poppler install failed"; exit 1; }
  b_grn "poppler installed."
fi

# ----------------------------- 3. Python 3.10/3.11/3.12 -------------------
b_yel "Step 3 — Python interpreter"
PY=""
for cand in python3.11 python3.12 python3.10; do
  if command -v "$cand" >/dev/null 2>&1; then
    PY="$cand"
    b_grn "Found $cand at $(command -v $cand)"
    "$cand" --version
    break
  fi
done

if [[ -z "$PY" ]]; then
  b_inf "No supported Python (3.10/3.11/3.12) found — installing python@3.11 via Homebrew..."
  brew install python@3.11 || { note_fail "python@3.11 install failed"; exit 1; }
  PY="python3.11"
  if ! command -v python3.11 >/dev/null 2>&1; then
    note_fail "python3.11 still not on PATH after brew install. You may need to: brew link --force python@3.11"
    exit 1
  fi
  b_grn "python3.11 installed: $(python3.11 --version)"
fi

# ----------------------------- 4. venv ------------------------------------
b_yel "Step 4 — virtual environment (.venv)"
if [[ -d .venv ]]; then
  b_inf ".venv already exists — reusing."
else
  b_inf "Creating .venv with $PY..."
  "$PY" -m venv .venv || { note_fail "venv create failed"; exit 1; }
  b_grn ".venv created."
fi
# shellcheck source=/dev/null
source .venv/bin/activate
b_inf "Active python: $(python --version)  at  $(which python)"

# ----------------------------- 5. pip / wheel upgrade ---------------------
b_yel "Step 5 — upgrade pip + wheel + setuptools"
python -m pip install --quiet --upgrade pip wheel setuptools || {
  note_fail "pip upgrade failed (continuing — pip may still work)"
}
b_grn "pip $(pip --version | awk '{print $2}')"

# ----------------------------- 6. requirements.txt ------------------------
b_yel "Step 6 — pip install -r requirements.txt"
b_inf "This installs 26 Phase 1 packages plus three SciSpaCy / spaCy model wheels (~600 MB)."
b_inf "On a fresh machine it takes 3-5 minutes; on re-runs it's seconds."
echo
pip install -r requirements.txt || {
  note_fail "pip install -r requirements.txt failed — check the error above."
  exit 1
}
b_grn "Phase 1 deps installed."

# ----------------------------- 7. .env template ---------------------------
b_yel "Step 7 — .env (env vars)"
if [[ -f .env ]]; then
  b_grn ".env already exists — leaving it alone."
else
  cp .env.example .env
  b_grn ".env created from .env.example."
  b_inf "Edit it later to fill in real GCP_PROJECT_ID / DocAI / BigQuery / GCS values."
fi

# ----------------------------- 8. verify ----------------------------------
b_yel "Step 8 — verify_environment.py --quick"
python scripts/verify_environment.py --quick || true   # don't abort on per-check fails; we report below
VERIFY_EC=$?

echo
echo "============================================================"
if [[ $failures -eq 0 && $VERIFY_EC -eq 0 ]]; then
  b_grn "Setup complete and environment verified. Ready for Phase 1."
  echo
  b_inf "Next steps:"
  b_inf "  - To use the venv in a fresh terminal:  source .venv/bin/activate"
  b_inf "  - To run the full cloud check (after gcloud auth):"
  b_inf "      python scripts/verify_environment.py"
  exit 0
else
  b_red "Setup finished with $failures script failure(s) and verify exit code $VERIFY_EC."
  b_inf "Review the lines above marked ✗. Common fixes are in README.md § Install."
  exit 1
fi
