# extractor — Makefile
#
# Conventions:
#   - PY points at the venv's python; no need to `source .venv/bin/activate`.
#   - All targets run from the repo root.
#   - `make help` lists every target with one-line descriptions.
#
# Usage examples:
#   make setup                # one-shot: install Python + deps + verify
#   make install              # pip install -r requirements.txt
#   make verify               # local environment check (no cloud calls)
#   make verify-cloud         # full check including DocAI / GCS / Gemini pings
#   make lint                 # ruff check
#   make test                 # pytest
#   make run-local PDF=gs://patient_clinical_trial/patient_profiles/demo.pdf
#   make ui PHASE=1           # streamlit run ui/phase1/app.py
#   make snapshot PHASE=1
#   make clean

.DEFAULT_GOAL := help
.PHONY: help setup install install-phase2 venv \
        verify verify-cloud verify-phase2 verify-phase3 \
        lint lint-fix format typecheck test test-cov \
        run-local score to-production ui portal snapshot \
        clean clean-cache clean-venv

# ----- variables --------------------------------------------------------------
VENV          := .venv
PY            := $(VENV)/bin/python
PIP           := $(VENV)/bin/pip
RUFF          := $(VENV)/bin/ruff
MYPY          := $(VENV)/bin/mypy
PYTEST        := $(VENV)/bin/pytest
STREAMLIT     := $(VENV)/bin/streamlit

PHASE         ?= 1
PDF           ?=
DOC           ?=
EXTRACTION    ?=
THRESHOLD     ?= 0.80

# ----- help -------------------------------------------------------------------
help:  ## Show available targets
	@printf "\n\033[1mextractor — make targets\033[0m\n\n"
	@awk 'BEGIN{FS=":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf "\n  Variables: PHASE=1 (UI / snapshot), PDF=gs://...  (run-local)\n\n"

# ----- one-shot setup ---------------------------------------------------------
setup:  ## Full Mac setup (Homebrew prereqs + venv + pip install + verify)
	bash setup.sh

# ----- venv + install ---------------------------------------------------------
venv:  ## Create the .venv with python3.12 (or 3.11 / 3.10 if found)
	@if [ ! -d $(VENV) ]; then \
	  PY=$$(command -v python3.12 || command -v python3.11 || command -v python3.10); \
	  if [ -z "$$PY" ]; then echo "No supported Python found (3.10-3.12)"; exit 1; fi; \
	  $$PY -m venv $(VENV); \
	  $(PIP) install --upgrade pip wheel setuptools; \
	fi

install: venv  ## pip install -r requirements.txt (Phase 1 essentials + spaCy models)
	$(PIP) install -r requirements.txt

install-phase2: install  ## pip install -r requirements-phase2.txt (medspacy + hgvs/cdot; all optional — tools degrade gracefully)
	$(PIP) install -r requirements-phase2.txt

fetch-hgnc:  ## Refresh config/data/hgnc_aliases.tsv from the HGNC complete set (keeps the seed on failure)
	$(PY) scripts/fetch_hgnc.py

# ----- verification -----------------------------------------------------------
verify:  ## Local environment check (no cloud calls)
	$(PY) scripts/verify_environment.py --quick

verify-cloud:  ## Full check including DocAI / GCS / Gemini pings
	$(PY) scripts/verify_environment.py

verify-phase2:  ## Phase 2 milestone gates (run every scripts/gates/gate_p2_*.py in order)
	@for g in $$(ls scripts/gates/gate_p2_*.py 2>/dev/null | sort -V); do \
	  echo "=== $$g ==="; PYTHONPATH=. $(PY) $$g || exit 1; \
	done

verify-phase3:  ## Phase 3 milestone gates (run every scripts/gates/gate_p3_*.py in order)
	@for g in $$(ls scripts/gates/gate_p3_*.py 2>/dev/null | sort -V); do \
	  echo "=== $$g ==="; PYTHONPATH=. $(PY) $$g || exit 1; \
	done

# ----- code quality -----------------------------------------------------------
lint:  ## Ruff lint check
	$(RUFF) check .

lint-fix:  ## Ruff lint with safe auto-fixes
	$(RUFF) check --fix .

format:  ## Ruff format (in place)
	$(RUFF) format .

typecheck:  ## Mypy strict type check on core/, preprocess/, agents/, teams/, etc.
	$(MYPY) core preprocess agents teams verification decision pipeline scripts

# ----- tests ------------------------------------------------------------------
test:  ## Run pytest
	$(PYTEST)

test-cov:  ## Run pytest with coverage report
	$(PYTEST) --cov=core --cov=preprocess --cov=agents --cov=teams \
	          --cov=verification --cov=decision --cov=pipeline \
	          --cov-report=term-missing

# ----- pipeline ---------------------------------------------------------------
run-local:  ## Run the pipeline on one PDF — usage: make run-local PDF=gs://... OR PDF=./local.pdf
	@if [ -z "$(PDF)" ]; then \
	  echo "Usage: make run-local PDF=gs://patient_clinical_trial/patient_profiles/demo.pdf  # from GCS"; \
	  echo "       make run-local PDF=./data/actual_docs/demo.pdf                            # local file"; \
	  exit 1; \
	fi
	@case "$(PDF)" in \
	  gs://*) FLAG=--gcs-uri ;; \
	  *)      FLAG=--local-pdf ;; \
	esac; \
	$(PY) scripts/process_local.py $$FLAG "$(PDF)" --version v$(PHASE)

score:  ## Score an extraction against ground_truth — usage: make score DOC=demo [EXTRACTION=path.json] [THRESHOLD=0.80]
	@if [ -z "$(DOC)" ]; then \
	  echo "Usage: make score DOC=demo                          # auto-detects backend (PERSISTENCE_BACKEND env)"; \
	  echo "       make score DOC=demo EXTRACTION=run.json      # reads from explicit local file"; \
	  echo "       make score DOC=demo THRESHOLD=0.90           # custom threshold"; \
	  exit 1; \
	fi
	@if [ -n "$(EXTRACTION)" ]; then \
	  $(PY) scripts/score_against_ground_truth.py --doc "$(DOC)" --extraction-file "$(EXTRACTION)" --threshold $(THRESHOLD); \
	else \
	  $(PY) scripts/score_against_ground_truth.py --doc "$(DOC)" --threshold $(THRESHOLD); \
	fi

to-production:  ## Convert a doc's extraction → production schema — usage: make to-production DOC=demo
	@if [ -z "$(DOC)" ]; then echo "Usage: make to-production DOC=demo  (add ARGS=--raw to ignore the SME-reviewed copy)"; exit 1; fi
	PYTHONPATH=. $(PY) scripts/to_production.py --doc "$(DOC)" $(ARGS)

ui:  ## Launch the Streamlit results browser — usage: make ui PHASE=1
	$(STREAMLIT) run ui/phase$(PHASE)/app.py \
	  --browser.gatherUsageStats=false \
	  --server.headless=true \
	  --server.port=8501 \
	  --server.runOnSave=true

portal:  ## Launch the Flask SME Review Portal — usage: make portal [PORT=8600]
	PYTHONPATH=. PORT=$${PORT:-8501} $(PY) ui/app.py

snapshot:  ## Snapshot a phase to snapshots/phaseN/ — usage: make snapshot PHASE=1
	$(PY) scripts/snapshot_phase.py --phase $(PHASE)

# ----- cleanup ----------------------------------------------------------------
clean: clean-cache  ## Remove caches (keep .venv)

clean-cache:  ## Remove __pycache__ / .pytest_cache / .mypy_cache / .ruff_cache
	find . -type d \( -name "__pycache__" -o -name "*.egg-info" \) -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov

clean-venv:  ## Wipe .venv entirely (forces full reinstall on next `make install`)
	rm -rf $(VENV)
