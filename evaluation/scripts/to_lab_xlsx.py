"""Convert `extraction_production.json` into a lab-layout XLSX.

This is the STANDALONE companion to the eval pipeline:
  • `python -m evaluation` (cli.py) produces `mismatches.xlsx` — a colored
    workbook that REQUIRES ground truth to compute per-cell verdicts.
  • THIS script writes a plain lab-layout workbook from extraction alone — no
    GT needed, no color coding. Useful for inspecting an extraction quickly,
    handing it to a clinician/SME for fresh review, or seeding a GT.

Column layout (20 columns: Biomarker / Method / Result / … / Exon) is read from
`evaluation/config/field_map.yaml` — the single source of truth shared with the
eval flow. The same `_cells_from_record` helper reused, so this script and
the eval flow agree by construction.

Usage:
    # one doc
    python -m evaluation.scripts.to_lab_xlsx --doc full_report_Redacted

    # every doc with an extraction_production.json
    python -m evaluation.scripts.to_lab_xlsx --batch

    # custom output path
    python -m evaluation.scripts.to_lab_xlsx --doc full_report_Redacted \
        --out /tmp/full_report_review.xlsx

Default output:  local_runs/artifacts/<doc>/review.xlsx
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Iterable

import yaml

from evaluation.lib.load_extraction import load_extraction

logger = logging.getLogger("evaluation.to_lab_xlsx")


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────


def _repo_root() -> Path:
    """The repo root, derived from this file's location
    (evaluation/scripts/to_lab_xlsx.py → up 2 levels)."""
    return Path(__file__).resolve().parent.parent.parent


def _default_extraction_path(doc_id: str) -> Path:
    return _repo_root() / "local_runs" / "artifacts" / doc_id / "extraction_production.json"


def _default_out_path(doc_id: str) -> Path:
    return _repo_root() / "local_runs" / "artifacts" / doc_id / "review.xlsx"


def _load_field_map() -> dict:
    p = _repo_root() / "evaluation" / "config" / "field_map.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


# ─────────────────────────────────────────────────────────────────────────────
# Workbook writer
# ─────────────────────────────────────────────────────────────────────────────


# Lab-convention colors. NOT verdict-based — these are just nice header chrome.
_HEADER_FG = "FFFFFF"
_HEADER_BG = "305496"
# Faint banding to separate the two sections visually (no verdict semantics).
_VARIANT_BAND = "EAF2F8"     # very light blue
_BIOMARKER_BAND = "FDF6E3"   # very light cream


def write_lab_xlsx(
    extraction_path: Path,
    out_path: Path,
    field_map: dict,
) -> tuple[int, int]:
    """Read `extraction_path` and write a lab-layout workbook to `out_path`.

    Returns (variant_row_count, biomarker_row_count) for logging.
    """
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as exc:
        raise SystemExit(
            "openpyxl is required to write XLSX. Install with: pip install openpyxl"
        ) from exc

    # 1) Load extraction → list of ExtractedRow (section, cells).
    rows = load_extraction(extraction_path, field_map)
    variant_rows = [r for r in rows if r.section == "variants"]
    biomarker_rows = [r for r in rows if r.section == "biomarkers"]

    # 2) Column order — single source of truth.
    columns = [
        str(c.get("header") or "")
        for c in (field_map.get("columns") or [])
        if c.get("header")
    ]

    # 3) Build workbook.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Review Results"

    header_font = Font(bold=True, color=_HEADER_FG)
    header_fill = PatternFill(start_color=_HEADER_BG, end_color=_HEADER_BG, fill_type="solid")
    header_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    for col_idx, header in enumerate(columns, start=1):
        c = ws.cell(row=1, column=col_idx, value=header)
        c.font = header_font
        c.fill = header_fill
        c.alignment = header_align

    # Width derived from header length — generic over any column count.
    for col_idx, header in enumerate(columns, start=1):
        col_letter = openpyxl.utils.get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = max(10, min(30, len(header) + 6))
    ws.freeze_panes = "A2"

    # 4) Write rows — variants first, then biomarkers (same ordering convention
    #    as bootstrap_gt and the eval Review Results sheet). Faint banding
    #    distinguishes sections visually; this is NOT verdict coloring.
    excel_row = 2
    variant_fill = PatternFill(start_color=_VARIANT_BAND, end_color=_VARIANT_BAND, fill_type="solid")
    biomarker_fill = PatternFill(start_color=_BIOMARKER_BAND, end_color=_BIOMARKER_BAND, fill_type="solid")

    for r in variant_rows:
        for col_idx, header in enumerate(columns, start=1):
            cell = ws.cell(row=excel_row, column=col_idx, value=r.cells.get(header))
            cell.fill = variant_fill
        excel_row += 1
    for r in biomarker_rows:
        for col_idx, header in enumerate(columns, start=1):
            cell = ws.cell(row=excel_row, column=col_idx, value=r.cells.get(header))
            cell.fill = biomarker_fill
        excel_row += 1

    # 5) Save.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return len(variant_rows), len(biomarker_rows)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _discover_batch() -> Iterable[str]:
    """Yield doc IDs that have an `extraction_production.json` artifact."""
    root = _repo_root()
    art_dir = root / "local_runs" / "artifacts"
    if not art_dir.is_dir():
        return []
    seen: list[str] = []
    for sub in sorted(art_dir.iterdir()):
        if sub.is_dir() and (sub / "extraction_production.json").is_file():
            seen.append(sub.name)
    return seen


def _convert_one(
    doc_id: str,
    extraction_path: Path,
    out_path: Path,
    field_map: dict,
) -> int:
    if not extraction_path.is_file():
        logger.error("extraction file not found: %s", extraction_path)
        return 2
    n_var, n_bio = write_lab_xlsx(extraction_path, out_path, field_map)
    logger.info(
        "to_lab_xlsx[%s]: wrote %s  (variants=%d, biomarkers=%d)",
        doc_id, out_path, n_var, n_bio,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m evaluation.scripts.to_lab_xlsx",
        description=(
            "Convert extraction_production.json to a lab-layout XLSX (no GT, "
            "no color coding). Companion to the eval `mismatches.xlsx` flow."
        ),
    )
    ap.add_argument("--doc", help="doc_id (e.g. full_report_Redacted). Required unless --batch.")
    ap.add_argument("--extraction", type=Path, default=None,
                    help="Override path to extraction_production.json")
    ap.add_argument("--out", type=Path, default=None,
                    help="Override output path (default: local_runs/artifacts/<doc>/review.xlsx)")
    ap.add_argument("--batch", action="store_true",
                    help="Convert every doc with an extraction_production.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    field_map = _load_field_map()

    if args.batch:
        rc = 0
        for doc_id in _discover_batch():
            ext = _default_extraction_path(doc_id)
            out = _default_out_path(doc_id)
            rc |= _convert_one(doc_id, ext, out, field_map)
        return rc

    if not args.doc:
        ap.error("--doc is required (or use --batch)")

    ext = args.extraction or _default_extraction_path(args.doc)
    out = args.out or _default_out_path(args.doc)
    return _convert_one(args.doc, ext, out, field_map)


if __name__ == "__main__":
    sys.exit(main())
