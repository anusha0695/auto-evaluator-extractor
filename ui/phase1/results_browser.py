"""Results browser — the sidebar list of all processed docs."""

from __future__ import annotations

import streamlit as st

from ui.phase1.data_layer import RunSummary, active_backend, list_runs


_VERDICT_BADGE = {
    "auto_accept": "🟢",
    "sme_flag":    "🟡",
    "errored":     "🔴",
}


def render() -> RunSummary | None:
    """Render the sidebar list. Returns the selected RunSummary or None."""
    backend = active_backend()
    st.sidebar.markdown("### 📋 Processed documents")
    st.sidebar.caption(f"Backend: **{backend}**")

    if st.sidebar.button("🔄 Reload", use_container_width=True):
        st.cache_data.clear()

    runs = _cached_list_runs()
    if not runs:
        st.sidebar.info(
            "No processed runs yet.\n\n"
            "Run the pipeline first:\n"
            "```\nmake run-local PDF=/path/to/file.pdf\n```"
        )
        return None

    # Collapse to ONE entry per document (latest run wins). Artifacts are
    # stored per-doc and overwritten each run, so the views always show the
    # latest state of a doc — listing every historical run row would be
    # misleading (selecting an older run can't load its overwritten
    # artifacts). `runs` is already most-recent-first.
    latest_by_doc: dict[str, RunSummary] = {}
    run_counts: dict[str, int] = {}
    for r in runs:
        run_counts[r.doc_id] = run_counts.get(r.doc_id, 0) + 1
        if r.doc_id not in latest_by_doc:
            latest_by_doc[r.doc_id] = r
    docs = list(latest_by_doc.values())

    labels: list[str] = []
    for r in docs:
        badge = _VERDICT_BADGE.get(r.verdict or "", "⚪")
        ts = (r.completed_at or "")[:19].replace("T", " ")
        n = run_counts.get(r.doc_id, 1)
        runs_note = f" · {n} runs" if n > 1 else ""
        labels.append(f"{badge}  **{r.doc_id}**  · {ts}{runs_note}")

    selected_idx = st.sidebar.radio(
        f"{len(docs)} document(s):",
        options=range(len(docs)),
        format_func=lambda i: labels[i],
        label_visibility="visible",
        index=0,
    )
    return docs[selected_idx]


@st.cache_data(ttl=10)
def _cached_list_runs() -> list[RunSummary]:
    return list_runs(limit=200)
