"""
extractor — Phase 1 Streamlit results browser.

Read-only view of pipeline runs persisted to either:
  PERSISTENCE_BACKEND=local    → local_runs/*.jsonl + artifacts/<doc>/*.json
  PERSISTENCE_BACKEND=bigquery → BigQuery + GCS

Launch:
    make ui PHASE=1
    # or:
    streamlit run ui/phase1/app.py

The app NEVER triggers pipeline runs — it's a results browser only. To
process a new doc, run the pipeline from the terminal (`make run-local`)
and hit "🔄 Reload" in the sidebar.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make repo-root imports work regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env so PERSISTENCE_BACKEND etc. are available.
import core.env_loader  # noqa: E402, F401

import streamlit as st  # noqa: E402

from ui.phase1.data_layer import load_extraction  # noqa: E402
from ui.phase1.views import (  # noqa: E402
    explorer_view,
    metadata_extraction_view,
    overview,
)
from ui.phase1.results_browser import render as render_sidebar  # noqa: E402


def main() -> None:
    st.set_page_config(
        page_title="extractor — Phase 1",
        page_icon="🧬",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # ----- Header --------------------------------------------------------
    st.markdown(
        '<h1 style="margin-bottom:0">🧬 extractor — Phase 1</h1>'
        '<p style="color:#6b7280;margin-top:4px">Genomic / pathology PDF '
        '→ structured ground truth · MetadataTeam (one umbrella section)</p>',
        unsafe_allow_html=True,
    )

    # ----- Sidebar with run picker --------------------------------------
    run = render_sidebar()
    if run is None:
        st.info(
            "👈 Pick a run from the sidebar, or run the pipeline first:\n\n"
            "```bash\n"
            "make run-local PDF=/path/to/file.pdf\n"
            "```\n\n"
            "Then hit **🔄 Reload** in the sidebar."
        )
        st.stop()

    # ----- Page tabs -----------------------------------------------------
    extraction = load_extraction(run.doc_id)

    tab_explorer, tab_overview, tab_meta = st.tabs(
        ["🧩 Block explorer", "📄 Overview", "🔍 Metadata extraction"]
    )

    with tab_explorer:
        explorer_view.render(run=run)

    with tab_overview:
        overview.render(run=run, extraction=extraction)

    with tab_meta:
        metadata_extraction_view.render(run=run, extraction=extraction)


if __name__ == "__main__":
    main()
