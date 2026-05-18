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

    # Build short labels for the radio.
    labels: list[str] = []
    for r in runs:
        badge = _VERDICT_BADGE.get(r.verdict or "", "⚪")
        ts = (r.completed_at or "")[:19].replace("T", " ")
        labels.append(f"{badge}  **{r.doc_id}**  · {ts}")

    selected_idx = st.sidebar.radio(
        f"{len(runs)} run(s):",
        options=range(len(runs)),
        format_func=lambda i: labels[i],
        label_visibility="visible",
        index=0,
    )
    return runs[selected_idx]


@st.cache_data(ttl=10)
def _cached_list_runs() -> list[RunSummary]:
    return list_runs(limit=200)
