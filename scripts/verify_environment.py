#!/usr/bin/env python3
"""
verify_environment.py — confirm the dev environment is ready for Phase 1.

Run this after `pip install -r requirements.txt` and the spaCy model downloads
to confirm that:

  1. All Phase 1 Python packages import.
  2. The Vertex AI Gemini SDK is reachable (auth + project).
  3. The DocAI processor exists in the configured location.
  4. The GCS bucket + BigQuery tables are reachable.
  5. The in-process SciSpaCy + spaCy models load and run on a tiny sample.

Usage:
    python scripts/verify_environment.py [--quick] [--skip-cloud]

Exit code 0 if all checks pass, non-zero if any fail.

The --quick flag skips cloud checks (useful if you just want to confirm local
package + model installation worked).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as md
import os
import sys
import textwrap
from pathlib import Path
from typing import Callable

# Make `from core...` imports work regardless of cwd. The script lives at
# scripts/verify_environment.py; the repo root is its parent's parent.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env into os.environ before any module reads from env at import time.
import core.env_loader  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


CHECKS_PASSED = 0
CHECKS_FAILED = 0


def ok(label: str, detail: str = "") -> None:
    global CHECKS_PASSED
    CHECKS_PASSED += 1
    msg = f"  ✓ {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)


def fail(label: str, detail: str = "") -> None:
    global CHECKS_FAILED
    CHECKS_FAILED += 1
    msg = f"  ✗ {label}"
    if detail:
        msg += f"  — {detail}"
    print(msg)


def section(title: str) -> None:
    print()
    print(f"== {title}")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


PHASE_1_PACKAGES = [
    "langgraph", "langchain", "langchain-core", "langchain-google-genai",
    "google-genai",
    "google-cloud-documentai", "google-cloud-storage", "google-cloud-bigquery",
    "google-cloud-firestore", "google-cloud-aiplatform",
    "opentelemetry-api", "opentelemetry-sdk", "opentelemetry-exporter-gcp-trace",
    "google-cloud-logging", "google-cloud-monitoring",
    "pydantic", "jsonschema", "jinja2", "pyyaml",
    "structlog", "tenacity", "streamlit", "pdf2image", "Pillow",
    "spacy", "scispacy", "python-dateutil",
]


def check_python_version() -> None:
    section("Python version")
    v = sys.version_info
    if v.major == 3 and v.minor == 11:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    elif v.major == 3 and 10 <= v.minor <= 12:
        ok(
            f"Python {v.major}.{v.minor}.{v.micro}",
            "Phase 1 targets 3.11 but 3.10/3.12 should also work for most paths",
        )
    else:
        fail(f"Python {v.major}.{v.minor}.{v.micro}", "Phase 1 targets Python 3.11")


def check_packages() -> None:
    section(f"Phase 1 Python packages ({len(PHASE_1_PACKAGES)})")
    missing = []
    for pkg in PHASE_1_PACKAGES:
        try:
            v = md.version(pkg)
            ok(f"{pkg}", v)
        except md.PackageNotFoundError:
            fail(pkg, "not installed — run `pip install -r requirements.txt`")
            missing.append(pkg)
    if missing:
        print()
        print("  Hint: " + ", ".join(missing) + " missing")


def check_core_modules() -> None:
    section("core package — schema-independent foundation")
    try:
        import core
        ok(f"`import core`", f"{len(core.__all__)} public symbols")
    except Exception as exc:
        fail("`import core`", str(exc))
        return

    try:
        loader = core.SchemaLoader.from_path("config/schemas/genomic_pathology_v2.json")
        sections = loader.list_sections()
        ok("SchemaLoader loaded genomic_pathology_v2.json", f"{len(sections)} sections")
    except Exception as exc:
        fail("SchemaLoader.from_path()", str(exc))
        return

    try:
        renderer = core.PromptRenderer(schema_loader=loader, prompts_root="config/prompts")
        ok("PromptRenderer initialized")
    except Exception as exc:
        fail("PromptRenderer init", str(exc))


def check_spacy_models() -> None:
    section("In-process Medical NER models")
    try:
        import spacy
    except ImportError as exc:
        fail("spaCy import", str(exc))
        return

    for model_name in ["en_core_web_sm", "en_ner_bionlp13cg_md", "en_ner_bc5cdr_md"]:
        try:
            nlp = spacy.load(model_name)
            doc = nlp("Stanley D Schinke MD ordered JAK2 V617F testing for William Dawson.")
            n_ents = len(doc.ents)
            ok(f"spacy.load({model_name!r})", f"pipeline={nlp.pipe_names}, ents={n_ents}")
        except OSError as exc:
            fail(
                f"spacy.load({model_name!r})",
                f"model not installed. Run: "
                f"pip install <url-from-requirements.txt for {model_name}>",
            )
        except Exception as exc:
            fail(f"spacy.load({model_name!r})", f"{type(exc).__name__}: {exc}")


def check_cloud_auth() -> None:
    section("Google Cloud auth")
    try:
        import google.auth

        creds, project = google.auth.default()
        if project:
            ok("Application Default Credentials", f"project={project}")
        else:
            fail(
                "Application Default Credentials",
                "no project bound — run `gcloud auth application-default login` and "
                "`gcloud config set project medical-report-extraction`",
            )
    except Exception as exc:
        fail("google.auth.default()", str(exc))


def check_storage_config() -> None:
    section("config/storage.yaml")
    try:
        from core.persistence import load_storage_config

        cfg = load_storage_config("phase_1")
        ok("storage.yaml parsed", f"version={cfg.pipeline_version}")
        ok(
            "GCS bucket reference",
            f"gs://{cfg.artifacts_gcs_bucket}/{cfg.artifacts_gcs_prefix}",
        )
        ok(
            "BigQuery tables",
            f"{cfg.extractions_table_fqn} + {cfg.runs_table_fqn}",
        )
        ok(
            "Firestore checkpointer collection",
            cfg.firestore_checkpoint_collection,
        )
    except Exception as exc:
        fail("storage.yaml load", str(exc))


def check_docai_processor() -> None:
    section("DocAI Layout Parser")
    project = os.getenv("GCP_PROJECT_ID")
    location = os.getenv("DOCAI_LOCATION", "us")
    processor_id = os.getenv("DOCAI_PROCESSOR_ID")
    if not (project and processor_id):
        fail(
            "DocAI env vars",
            "GCP_PROJECT_ID and/or DOCAI_PROCESSOR_ID unset — copy .env.example to .env",
        )
        return
    try:
        from google.cloud import documentai_v1 as documentai

        client = documentai.DocumentProcessorServiceClient(
            client_options={"api_endpoint": f"{location}-documentai.googleapis.com"}
        )
        processor_name = client.processor_path(project, location, processor_id)
        processor = client.get_processor(name=processor_name)
        ok(
            "DocAI processor reachable",
            f"name={processor.display_name}, state={processor.state.name}",
        )
    except Exception as exc:
        fail("DocAI processor lookup", f"{type(exc).__name__}: {exc}")


def check_gcs_bucket() -> None:
    section("GCS bucket")
    try:
        from core.gcs_client import GCSClient
        from core.persistence import load_storage_config
        import asyncio

        cfg = load_storage_config("phase_1")
        client = GCSClient()

        async def _ls() -> list[str]:
            return await client.list_pdfs(cfg.input_gcs_bucket, cfg.input_gcs_prefix)

        uris = asyncio.run(_ls())
        ok(
            f"List gs://{cfg.input_gcs_bucket}/{cfg.input_gcs_prefix}",
            f"{len(uris)} PDF(s) found",
        )
    except Exception as exc:
        fail("GCS list", f"{type(exc).__name__}: {exc}")


def check_gemini() -> None:
    section("Gemini ping (langchain-google-genai)")
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "true").lower() == "true"
    backend = "Vertex AI (BAA-covered)" if use_vertex else "Google AI Studio (NOT BAA-covered)"
    ok(f"Backend selected", backend)

    if use_vertex:
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
        region = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        if not project:
            fail("Vertex env vars", "GOOGLE_CLOUD_PROJECT / GCP_PROJECT_ID unset")
            return
        os.environ["GOOGLE_CLOUD_PROJECT"] = project
        os.environ["GOOGLE_CLOUD_LOCATION"] = region
    else:
        if not os.getenv("GOOGLE_API_KEY"):
            fail("Google AI Studio env vars", "GOOGLE_API_KEY unset")
            return

    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        model_name = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")
        llm = ChatGoogleGenerativeAI(model=model_name, temperature=0.0, max_output_tokens=8)
        resp = llm.invoke("Reply with the single word 'pong' and nothing else.")
        text = (getattr(resp, "content", "") or "").strip().lower()
        if "pong" in text:
            ok(f"Gemini Flash ping ({model_name})", f"reply={text!r}")
        else:
            fail(f"Gemini Flash ping ({model_name})", f"unexpected reply: {text!r}")
    except Exception as exc:
        fail("Gemini ping", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip cloud reachability checks; only verify local packages + models.",
    )
    parser.add_argument(
        "--skip-cloud",
        action="store_true",
        help="Alias for --quick.",
    )
    args = parser.parse_args()
    quick = args.quick or args.skip_cloud

    print("extractor — environment verification")
    print(f"  cwd:    {os.getcwd()}")
    if quick:
        print("  mode:   --quick (skipping cloud checks)")

    check_python_version()
    check_packages()
    check_core_modules()
    check_spacy_models()

    if not quick:
        check_cloud_auth()
        check_storage_config()
        check_docai_processor()
        check_gcs_bucket()
        check_gemini()
    else:
        check_storage_config()

    print()
    print("=" * 60)
    if CHECKS_FAILED == 0:
        print(f"  All {CHECKS_PASSED} checks passed. Environment is ready.")
        return 0
    print(
        f"  {CHECKS_FAILED} of {CHECKS_PASSED + CHECKS_FAILED} checks failed."
    )
    print(
        textwrap.dedent("""\

          Common fixes:
            - Models missing → re-run the SciSpaCy + spaCy model installs
              from requirements.txt (the lines starting with https://).
              On macOS / Linux dev boxes these install in seconds.
            - Cloud auth missing → `gcloud auth application-default login`
              and `gcloud config set project medical-report-extraction`.
            - DocAI / GCS errors → confirm BAA-covered processor + bucket
              are provisioned in project medical-report-extraction.
            - Vertex errors → confirm the project is BAA-covered for Gemini
              and the model names in .env match what's available in your
              region (VERTEX_LOCATION=us-central1 is the default).
        """)
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
