"""
JSON tree renderer — wraps Streamlit's built-in JSON renderer with extra
affordances for the extractor's report_metadata:

  - VERBATIM / DERIVED tags on each field (pulled from the schema).
  - Per-field "expected vs actual" mode when a ground truth is provided.
  - Click-to-jump-to-page when a field has a page citation (Phase 3+).
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st


def render_json(data: Any, *, label: str | None = None, expanded: bool = True) -> None:
    """Simple wrapper around st.json with an optional caption."""
    if label:
        st.caption(label)
    st.json(data, expanded=expanded)


def render_report_metadata_table(
    *,
    extraction: dict[str, Any],
    ground_truth: dict[str, Any] | None = None,
) -> None:
    """Render report_metadata as a two-column table: field → value.

    If `ground_truth` is provided, adds an "Expected" column and ✓/✗ marks
    on each row. Useful for SME review.
    """
    skip_keys = {"llm_confidence_score"}

    rows: list[dict[str, Any]] = []
    for key, val in extraction.items():
        if key in skip_keys:
            continue
        row: dict[str, Any] = {
            "Field": key,
            "Extracted": _display_value(val),
        }
        if ground_truth is not None:
            exp = ground_truth.get(key)
            row["Expected"] = _display_value(exp)
            row["Match"] = "✅" if _values_match(exp, val) else "❌"
        rows.append(row)

    st.dataframe(rows, use_container_width=True, hide_index=True)


def _display_value(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


def _values_match(expected: Any, actual: Any) -> bool:
    """Lenient comparison matching the scoring script's policy."""
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    if expected == actual:
        return True
    if isinstance(expected, str) and isinstance(actual, str):
        return _norm(expected) == _norm(actual)
    return False


def _norm(s: str) -> str:
    return " ".join(s.lower().split())
