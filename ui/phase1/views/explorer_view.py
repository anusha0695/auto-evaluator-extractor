"""
explorer_view — the block-centric PDF explorer page (U11).

Renders the real PDF page(s) with per-block bbox overlays + an inspector,
driven entirely by the assembled BlockViews. Replaces the old abstract
preprocessing block-list view.
"""

from __future__ import annotations

import streamlit as st
import streamlit.components.v1 as components

from ui.phase1.block_view import build_block_views, find_source_pdf
from ui.phase1.components.explorer import build_explorer_html
from ui.phase1.page_render import render_pages


@st.cache_data(show_spinner=False)
def _rendered_pages(pdf_path_str: str) -> list[dict]:
    from pathlib import Path
    return render_pages(Path(pdf_path_str))


def render(*, run) -> None:
    doc_id = run.doc_id
    views = build_block_views(doc_id)

    if not views:
        st.warning(
            f"No `blocks.json` artifact for **{doc_id}**. Re-run the pipeline "
            "(`make run-local PDF=...`) so the explorer has block geometry to "
            "render, then hit Reload."
        )
        return

    n_multi = sum(
        1 for v in views
        if len([u for u in v.target_umbrella_hints if u != "none"]) > 1
    )
    n_bbox = sum(1 for v in views if v.bbox)
    st.caption(
        f"{len(views)} blocks · {n_bbox} with geometry · {n_multi} multi-umbrella "
        f"· entities and fields joined by block_id"
    )

    pdf_path = find_source_pdf(doc_id)
    pages: list[dict] = []
    if pdf_path:
        pages = _rendered_pages(str(pdf_path))
    if not pages:
        st.info(
            "Rendering the PDF page image needs the source PDF + poppler "
            "(`pdf2image`). Showing overlays on a blank canvas instead — "
            "boxes are positioned from real bbox coordinates."
        )

    html = build_explorer_html(views, pages, height=820)
    components.html(html, height=860, scrolling=True)

    # Blocks without geometry (image/empty) — listed so they aren't lost.
    no_geo = [v for v in views if not v.bbox]
    if no_geo:
        with st.expander(f"{len(no_geo)} block(s) without geometry (not drawable)"):
            for v in no_geo:
                st.markdown(
                    f"`{v.block_id}` · {v.text_role} · "
                    f"{', '.join(v.target_umbrella_hints)} — "
                    f"{(v.text or '(no text)')[:80]}"
                )
