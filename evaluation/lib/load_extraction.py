"""Load `extraction_production.json` into the flat (section, cells) row shape
the rest of the evaluation pipeline uses."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExtractedRow:
    section: str                              # "variants" | "biomarkers"
    cells: dict[str, Any] = field(default_factory=dict)   # column header → raw value
    record_index: int = -1                    # index in the JSON array (for ref backtracking)


def load_extraction(path: str | Path, field_map: dict[str, Any]) -> list[ExtractedRow]:
    """Read extraction_production.json and produce one row per emitted record.

    The field_map drives WHICH JSON path populates which column. Section is the
    one piece we know up front for extracted rows (variants[] vs biomarkers[]) —
    GT rows learn their section via row-matching, not from the file.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    inner = raw.get("genomic_pathology_extraction") or {}

    variants = (inner.get("Genomic_Variant_umbrella") or {}).get("Genomic_Variants") or []
    biomarkers = (inner.get("other_molecular_biomarker_umbrella") or {}).get("other_molecular_biomarkers") or []

    cols = field_map.get("columns") or []

    rows: list[ExtractedRow] = []
    for i, rec in enumerate(variants):
        if not isinstance(rec, dict):
            continue
        rows.append(ExtractedRow(section="variants", record_index=i,
                                  cells=_cells_from_record(rec, cols, "variants")))
    for i, rec in enumerate(biomarkers):
        if not isinstance(rec, dict):
            continue
        rows.append(ExtractedRow(section="biomarkers", record_index=i,
                                  cells=_cells_from_record(rec, cols, "biomarkers")))
    return rows


def _cells_from_record(record: dict[str, Any], columns: list[dict], section: str) -> dict[str, Any]:
    """For each column, pull the value at `sources[section]` (if non-empty
    field name) from the record. Stores under the column header.

    The `if header not in out` guard remains as a defensive fallback: if a
    future field_map ever reintroduces a duplicate header by mistake, the
    FIRST occurrence wins (matching the same rule in load_ground_truth, so
    the two sides stay symmetric). In the current field_map every header is
    unique.
    """
    out: dict[str, Any] = {}
    for col in columns:
        header = col.get("header") or ""
        src_field = (col.get("sources") or {}).get(section) or ""
        if not src_field:
            # column intentionally blank for this section
            out.setdefault(header, None)
            continue
        if header not in out:
            out[header] = record.get(src_field)
    return out
