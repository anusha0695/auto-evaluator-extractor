"""Load the SME-authored ground-truth XLSX into row records.

The GT workbook has one stacked sheet with the 20-column lab layout (per the
reference screenshots). The eval module does NOT pre-classify rows into
variants vs biomarkers — `match_rows.py` decides each row's section by
trying the extracted data first as a variant then as a biomarker (the
match-against-both strategy). So the loader just reads the rows verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class GTRow:
    cells: dict[str, Any] = field(default_factory=dict)   # column header → raw value
    sheet_row: int = -1                                    # 1-indexed Excel row (for diff_log)
    candidate_section: str = ""                           # filled in by match_rows.py


def load_ground_truth(
    path: str | Path,
    field_map: dict[str, Any],
    sheet_name: str | None = None,
) -> list[GTRow]:
    """Read the GT workbook.

    Sheet-selection rules (in order):
      1. If `sheet_name` is given AND present in the workbook, use it. This is
         the multi-sheet flow — one workbook with a sheet per doc_id.
      2. Else prefer a sheet literally named "Review Results" (lab convention).
      3. Else fall back to the first sheet in the workbook.

    The column headers in the workbook are matched against the field_map's
    `columns[].header` in order. If the workbook has extra columns past column
    T, they're ignored. If it's missing columns, those cells come back as None.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError(
            "openpyxl is required to load ground-truth XLSX. "
            "Install with: pip install openpyxl"
        ) from exc

    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)

    if sheet_name and sheet_name in wb.sheetnames:
        chosen = sheet_name
    elif "Review Results" in wb.sheetnames:
        chosen = "Review Results"
    else:
        chosen = wb.sheetnames[0]
    ws = wb[chosen]

    expected_headers = [col.get("header") or "" for col in (field_map.get("columns") or [])]

    rows_iter = ws.iter_rows(values_only=True)
    header_row = next(rows_iter, None)
    if header_row is None:
        return []

    # Trim trailing Nones from the header row, then build the column index map.
    actual_headers = [str(h).strip() if h is not None else "" for h in header_row]

    # Build a column-index lookup from the workbook header row. The current
    # field_map has unique headers; the FIRST-OCCURRENCE rule below is a
    # defensive fallback in case a future workbook reintroduces a duplicate
    # by mistake. It mirrors the same rule in load_extraction._cells_from_record
    # so both sides stay symmetric.
    header_to_first_idx: dict[str, int] = {}
    for idx, h in enumerate(actual_headers):
        if h and h not in header_to_first_idx:
            header_to_first_idx[h] = idx

    out: list[GTRow] = []
    for excel_row_no, row in enumerate(rows_iter, start=2):
        if row is None or all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
            continue
        cells: dict[str, Any] = {}
        for exp_h in expected_headers:
            if exp_h in header_to_first_idx:
                idx = header_to_first_idx[exp_h]
                cells[exp_h] = row[idx] if idx < len(row) else None
            else:
                cells[exp_h] = None
        out.append(GTRow(cells=cells, sheet_row=excel_row_no))
    return out
