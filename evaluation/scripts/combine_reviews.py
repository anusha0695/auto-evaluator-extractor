#!/usr/bin/env python3
"""Combine every ``review.xlsx`` under an artifacts folder into one workbook.

Given an artifacts folder, walk each immediate subfolder; if it contains a
``review.xlsx`` (produced by ``to_lab_xlsx.py``), add its contents as a new
sheet in a single combined workbook.

Sheet naming rule (per request):
    * take the part of the SUBFOLDER name AFTER the first hyphen ``-``;
    * if the name has no hyphen, use the whole folder name.
    * then sanitize for Excel (<=31 chars, drop ``[ ] : * ? / \``, de-dupe
      collisions with ``_2``, ``_3`` ...).

Fidelity: cell VALUES plus the per-row banding fills are copied, and the blue
header row + frozen top row + column widths are re-applied, so each combined
sheet looks like the original ``review.xlsx``.

Usage:
    # merge everything under an artifacts folder
    python -m evaluation.scripts.combine_reviews local_runs/artifacts

    # custom output + custom source filename
    python -m evaluation.scripts.combine_reviews /path/to/artifacts \
        --out /tmp/all_reviews.xlsx --filename review.xlsx

    # or as a plain script (standalone — only needs openpyxl):
    python evaluation/scripts/combine_reviews.py local_runs/artifacts

Default output:  <artifacts_folder>/combined_reviews.xlsx
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger("combine_reviews")

# Characters Excel forbids in a sheet title.
_INVALID_SHEET_CHARS = re.compile(r"[\[\]:\*\?/\\]")
_MAX_SHEET_LEN = 31

# Header chrome — matches to_lab_xlsx.py so combined sheets look identical.
_HEADER_FG = "FFFFFF"
_HEADER_BG = "305496"


def sheet_name_from_folder(folder_name: str) -> str:
    """Text after the first '-', else the whole name; sanitized (not yet de-duped)."""
    base = folder_name.split("-", 1)[1] if "-" in folder_name else folder_name
    base = _INVALID_SHEET_CHARS.sub("_", base).strip()
    if not base:
        base = "sheet"
    return base[:_MAX_SHEET_LEN]


def _dedupe(name: str, used: set[str]) -> str:
    """Return a unique sheet name, suffixing _2, _3, ... on collision."""
    if name not in used:
        used.add(name)
        return name
    i = 2
    while True:
        suffix = f"_{i}"
        cand = (name[: _MAX_SHEET_LEN - len(suffix)] + suffix)
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


def _copy_sheet(src_ws, dst_ws) -> None:
    """Copy values + row banding into dst_ws; re-apply header + freeze + widths."""
    from openpyxl.styles import Alignment, Font, PatternFill

    header_font = Font(bold=True, color=_HEADER_FG)
    header_fill = PatternFill(start_color=_HEADER_BG, end_color=_HEADER_BG, fill_type="solid")
    header_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    for row in src_ws.iter_rows():
        for cell in row:
            new = dst_ws.cell(row=cell.row, column=cell.column, value=cell.value)
            if cell.row == 1:
                new.font = header_font
                new.fill = header_fill
                new.alignment = header_align
            else:
                # Preserve the light variant/biomarker banding, if present.
                try:
                    fill = cell.fill
                    if fill is not None and fill.fill_type == "solid":
                        rgb = fill.fgColor.rgb
                        if rgb:
                            new.fill = PatternFill(start_color=rgb, end_color=rgb, fill_type="solid")
                except Exception:  # noqa: BLE001 — styling is best-effort, never fatal
                    pass

    # Column widths — copy from source when set, else leave default.
    for col_letter, dim in src_ws.column_dimensions.items():
        if dim.width:
            dst_ws.column_dimensions[col_letter].width = dim.width

    dst_ws.freeze_panes = "A2"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="combine_reviews",
        description="Merge every review.xlsx under an artifacts folder into one workbook "
        "(one sheet per doc; sheet name = folder name after the first hyphen).",
    )
    ap.add_argument("artifacts_folder", type=Path,
                    help="Parent folder whose immediate subfolders may hold a review.xlsx.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output workbook path (default: <artifacts_folder>/combined_reviews.xlsx).")
    ap.add_argument("--filename", default="review.xlsx",
                    help="Per-folder file to look for (default: review.xlsx).")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
    )

    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("openpyxl is required. Install with: pip install openpyxl") from exc

    root: Path = args.artifacts_folder
    if not root.is_dir():
        logger.error("not a directory: %s", root)
        return 2

    subfolders = sorted(p for p in root.iterdir() if p.is_dir())
    found = [(p, p / args.filename) for p in subfolders if (p / args.filename).is_file()]
    skipped = [p.name for p in subfolders if not (p / args.filename).is_file()]

    if not found:
        logger.warning("no %s found under %s — nothing to combine", args.filename, root)
        return 1

    out: Path = args.out or (root / "combined_reviews.xlsx")

    wb = openpyxl.Workbook()
    default_ws = wb.active
    used: set[str] = {default_ws.title}   # avoid colliding with the throwaway default sheet

    n = 0
    for folder, xlsx in found:
        name = _dedupe(sheet_name_from_folder(folder.name), used)
        src_wb = openpyxl.load_workbook(xlsx)
        try:
            _copy_sheet(src_wb.active, wb.create_sheet(title=name))
        finally:
            src_wb.close()
        n += 1
        logger.info("added sheet %-31s  <- %s", name, folder.name)

    # Drop the empty default sheet now that we have real ones.
    if default_ws in wb.worksheets and len(wb.worksheets) > 1:
        wb.remove(default_ws)

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))

    logger.info("combined %d review(s) -> %s", n, out)
    if skipped:
        logger.info("skipped %d subfolder(s) without %s: %s",
                    len(skipped), args.filename, ", ".join(skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
