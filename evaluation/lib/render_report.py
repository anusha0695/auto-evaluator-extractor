"""Render metrics → JSON / classification report / mismatches XLSX / summary MD.

The mismatches XLSX has FOUR sheets:
  - Review Results — lab-style layout with per-cell coloring
                     (green = TP, yellow = WRONG, pink = FN/FP).
                     This is the workbook the SME reviews first. Column
                     order comes from evaluation/config/field_map.yaml —
                     never hardcoded here (avoids the drift bug where
                     field_map says one thing and this file says another).
  - Wrong          — drill-down: every WRONG cell with gt + extracted side by side.
  - Missing        — every FN cell (GT has value, extraction empty).
  - Spurious       — every FP cell (extraction has value, GT empty).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluation.lib.compare_cells import FN, FP, TN, TP, WRONG


# Cell fill colors — matches the lab convention from the reference workbook.
_FILL_TP    = "C6EFCE"   # green
_FILL_WRONG = "FFEB9C"   # yellow
_FILL_FN    = "FFC7CE"   # pink (missing)
_FILL_FP    = "FCE4D6"   # light orange (spurious)
_HEADER_FG  = "FFFFFF"
_HEADER_BG  = "305496"


def _column_headers_from_field_map(field_map: dict[str, Any] | None) -> list[str]:
    """Return the ordered list of column headers from field_map.yaml.

    Single source of truth for the lab-layout column order. If `field_map`
    wasn't passed in (legacy caller), load it from
    evaluation/config/field_map.yaml so the column list still agrees with
    load_ground_truth / load_extraction.
    """
    fm = field_map
    if fm is None:
        import yaml
        # evaluation/lib/render_report.py → evaluation/config/field_map.yaml
        p = Path(__file__).resolve().parent.parent / "config" / "field_map.yaml"
        fm = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [str(c.get("header") or "") for c in (fm.get("columns") or []) if c.get("header")]


def write_all(
    out_dir: Path,
    doc_id: str,
    gt_path: Path,
    ex_path: Path,
    metrics: dict[str, Any],
    field_map: dict[str, Any] | None = None,
    file_prefix: str = "",
) -> None:
    """Write every output artifact to `out_dir`. Creates the dir if needed.

    `field_map` is the parsed evaluation/config/field_map.yaml. When provided,
    the Review Results sheet header order is taken from it (single source of
    truth). When omitted, falls back to loading the YAML from disk so legacy
    callers still work.

    `file_prefix` is prepended to every output filename. Defaults to "" so the
    legacy `--doc` / `--batch` flow keeps writing `metrics.json`, etc. The
    workbook flow passes `file_prefix="eval_"` so the four eval outputs land
    next to pipeline artifacts (extraction_production.json, link_metrics.json,
    …) without name collisions: `eval_metrics.json`, `eval_report.txt`,
    `eval_mismatches.xlsx`, `eval_summary.md`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cell_verdicts = metrics.pop("_cell_verdicts", [])

    p = file_prefix
    _write_metrics_json(out_dir / f"{p}metrics.json", doc_id, gt_path, ex_path, metrics)
    _write_classification_report(out_dir / f"{p}report.txt"
                                 if p else out_dir / "classification_report.txt",
                                 metrics)
    _write_mismatches_xlsx(out_dir / f"{p}mismatches.xlsx", doc_id, cell_verdicts,
                            metrics, field_map=field_map)
    _write_summary_md(out_dir / f"{p}summary.md", doc_id, metrics)


# ---------------------------------------------------------------------------

def _write_metrics_json(path: Path, doc_id: str, gt: Path, ex: Path,
                        metrics: dict[str, Any]) -> None:
    payload = {
        "doc_id": doc_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ground_truth_file": str(gt),
        "extraction_file": str(ex),
        **metrics,
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _write_classification_report(path: Path, metrics: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append(f"{'':<32}{'precision':>10}{'recall':>10}{'f1-score':>10}{'support':>10}")
    lines.append("")
    total_support = 0
    field_p_sum = field_r_sum = field_f_sum = 0.0
    fields_counted = 0
    for header, m in metrics.get("field_level", {}).items():
        support = m.get("tp", 0) + m.get("fn", 0) + m.get("wrong", 0)
        if support == 0 and m.get("fp", 0) == 0:
            continue
        lines.append(f"{header:<32}{m['precision']:>10.2f}{m['recall']:>10.2f}{m['f1']:>10.2f}{support:>10d}")
        total_support += support
        field_p_sum += m["precision"]; field_r_sum += m["recall"]; field_f_sum += m["f1"]
        fields_counted += 1

    n = max(fields_counted, 1)
    o = metrics.get("overall", {})
    lines.append("")
    lines.append(f"{'accuracy':<32}{'':>10}{'':>10}{o.get('f1', 0):>10.2f}{total_support:>10d}")
    lines.append(f"{'macro avg':<32}{field_p_sum/n:>10.2f}{field_r_sum/n:>10.2f}{field_f_sum/n:>10.2f}{total_support:>10d}")
    lines.append(f"{'weighted avg':<32}{o.get('precision', 0):>10.2f}{o.get('recall', 0):>10.2f}{o.get('f1', 0):>10.2f}{total_support:>10d}")
    lines.append("")
    lines.append("Row-level metrics:")
    for sec, rm in metrics.get("row_level", {}).items():
        lines.append(
            f"  {sec:<12} precision={rm['precision']:.3f}  recall={rm['recall']:.3f}  "
            f"f1={rm['f1']:.3f}  (gt={rm['gt']}, extracted={rm['extracted']}, "
            f"matched={rm['matched']}, missing={rm['missing']}, spurious={rm['spurious']})"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_mismatches_xlsx(path: Path, doc_id: str, cell_verdicts: list[dict],
                           metrics: dict[str, Any],
                           field_map: dict[str, Any] | None = None) -> None:
    """Write 4 sheets:
      - Review Results — lab layout, color-coded per verdict (matches the
                         SME's reference workbook). Column headers/order come
                         from field_map.yaml (single source of truth).
      - Wrong / Missing / Spurious — drill-down lists for analytic use.
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.comments import Comment
    except ImportError:
        path.with_suffix(".tsv").write_text(_verdicts_to_tsv(doc_id, cell_verdicts), encoding="utf-8")
        return

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # --------------------------------------------------------------
    # Sheet 1: Review Results — lab layout with per-cell coloring.
    # --------------------------------------------------------------
    _write_review_results_sheet(wb, doc_id, cell_verdicts,
                                 openpyxl, PatternFill, Font, Alignment, Comment,
                                 field_map=field_map)

    # --------------------------------------------------------------
    # Sheets 2-4: Wrong / Missing / Spurious drill-downs.
    # --------------------------------------------------------------
    headers = ["doc_id", "section", "identity", "column", "verdict",
               "gt_value", "gt_canon", "extracted_value", "ex_canon"]
    bands = {
        "Wrong":    ([v for v in cell_verdicts if v["verdict"] == WRONG], _FILL_WRONG),
        "Missing":  ([v for v in cell_verdicts if v["verdict"] == FN],    _FILL_FN),
        "Spurious": ([v for v in cell_verdicts if v["verdict"] == FP],    _FILL_FP),
    }
    bold = Font(bold=True)
    for sheet_name, (rows, fill_color) in bands.items():
        ws = wb.create_sheet(sheet_name)
        for col_idx, h in enumerate(headers, start=1):
            c = ws.cell(row=1, column=col_idx, value=h)
            c.font = bold
        for r_idx, v in enumerate(rows, start=2):
            ws.cell(row=r_idx, column=1, value=doc_id)
            ws.cell(row=r_idx, column=2, value=v.get("section"))
            ws.cell(row=r_idx, column=3, value=" / ".join(str(x) for x in v.get("identity") or ()))
            ws.cell(row=r_idx, column=4, value=v.get("column"))
            cell = ws.cell(row=r_idx, column=5, value=v.get("verdict"))
            cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
            ws.cell(row=r_idx, column=6, value=v.get("gt"))
            ws.cell(row=r_idx, column=7, value=v.get("gt_canon"))
            ws.cell(row=r_idx, column=8, value=v.get("extracted"))
            ws.cell(row=r_idx, column=9, value=v.get("ex_canon"))
        ws.column_dimensions["A"].width = 22
        ws.column_dimensions["B"].width = 12
        ws.column_dimensions["C"].width = 28
        ws.column_dimensions["D"].width = 24
        ws.column_dimensions["E"].width = 8
        ws.column_dimensions["F"].width = 28
        ws.column_dimensions["G"].width = 22
        ws.column_dimensions["H"].width = 28
        ws.column_dimensions["I"].width = 22

    if not wb.sheetnames:
        wb.create_sheet("Empty")
    wb.save(str(path))


def _write_review_results_sheet(wb, doc_id, cell_verdicts,
                                 openpyxl, PatternFill, Font, Alignment, Comment,
                                 field_map: dict[str, Any] | None = None,
                                 sheet_name: str = "Review Results",
                                 position: int | None = 0) -> None:
    """Render the lab-layout review sheet, each cell colored by its verdict.
    The SME opens this sheet first — it mirrors the reference workbook so the
    review experience is familiar.

    Column order is taken from field_map.yaml (single source of truth). If
    the caller didn't pass it in, fall back to loading the YAML from disk —
    so the column layout in this sheet, in load_ground_truth, in
    load_extraction, and in bootstrap_gt all agree by construction.

    `sheet_name` and `position` let callers reuse this rendering for both:
      • per-doc mismatches.xlsx — sheet_name="Review Results", position=0
        (the SME-facing first sheet of the per-doc drill-down workbook)
      • the consolidated `<input>_evaluated.xlsx` — sheet_name=<doc filename>,
        position=None (append). Each doc gets its own colored review sheet
        all in one workbook so the SME can switch via sheet tabs.

    Excel caps sheet names at 31 chars; we truncate defensively rather than
    raising — losing a tail beats failing the whole batch.
    """
    safe_name = (sheet_name or "Review Results")[:31]

    # Group verdicts by (section, identity) → {column_header: verdict_record}
    by_row: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for v in cell_verdicts:
        ident = tuple(v.get("identity") or ())
        by_row[(v.get("section"), ident)][v.get("column")] = v

    # Stable row ordering: variants first, then biomarkers; within section,
    # alphabetical by identity tuple.
    sorted_keys = sorted(by_row.keys(), key=lambda k: (
        0 if k[0] == "variants" else 1, tuple(str(x) for x in k[1])))

    columns = _column_headers_from_field_map(field_map)

    if position is None:
        ws = wb.create_sheet(safe_name)
    else:
        ws = wb.create_sheet(safe_name, position)
    header_font = Font(bold=True, color=_HEADER_FG)
    header_fill = PatternFill(start_color=_HEADER_BG, end_color=_HEADER_BG, fill_type="solid")
    header_align = Alignment(horizontal="left", vertical="center", wrap_text=True)
    for col_idx, header in enumerate(columns, start=1):
        c = ws.cell(row=1, column=col_idx, value=header)
        c.font = header_font
        c.fill = header_fill
        c.alignment = header_align
    ws.freeze_panes = "A2"
    # Column widths — derive from header content (no hardcoded letter map; the
    # column count is whatever field_map.yaml says). Long headers like
    # "Clinical Significance" get more room; short ones like "Method" / "Exon"
    # get less. This is intentionally heuristic, not pixel-perfect.
    for col_idx, header in enumerate(columns, start=1):
        col_letter = openpyxl.utils.get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = max(10, min(30, len(header) + 6))

    fills = {
        TP:    PatternFill(start_color=_FILL_TP,    end_color=_FILL_TP,    fill_type="solid"),
        WRONG: PatternFill(start_color=_FILL_WRONG, end_color=_FILL_WRONG, fill_type="solid"),
        FN:    PatternFill(start_color=_FILL_FN,    end_color=_FILL_FN,    fill_type="solid"),
        FP:    PatternFill(start_color=_FILL_FP,    end_color=_FILL_FP,    fill_type="solid"),
    }

    for r_idx, key in enumerate(sorted_keys, start=2):
        cells_by_col = by_row[key]
        for col_idx, header in enumerate(columns, start=1):
            v = cells_by_col.get(header)
            if v is None:
                continue       # no verdict recorded for this column (e.g. blank for section)
            verdict = v.get("verdict")
            # Display value: prefer extracted (what shipped); fall back to GT
            # so missing rows still show what SHOULD have been there.
            display = v.get("extracted") or v.get("gt") or ""
            cell = ws.cell(row=r_idx, column=col_idx, value=display)
            fill = fills.get(verdict)
            if fill is not None:
                cell.fill = fill
            # Add a comment that names the verdict + the OTHER side, so an SME
            # hovering on a colored cell sees `gt → extracted` (lab convention
            # for showing the normalization arrow).
            if verdict == WRONG:
                cell.comment = Comment(
                    f"WRONG\nGT: {v.get('gt')}\nExtracted: {v.get('extracted')}",
                    "Eval")
            elif verdict == FN:
                cell.comment = Comment(f"MISSING\nGT: {v.get('gt')}", "Eval")
                # For missing, show GT value so SME can see what was expected.
                cell.value = f"[MISSING] {v.get('gt') or ''}"
            elif verdict == FP:
                cell.comment = Comment(f"SPURIOUS\nExtracted: {v.get('extracted')}", "Eval")


def write_review_results_into(
    wb,
    doc_id: str,
    cell_verdicts: list[dict],
    sheet_name: str,
    field_map: dict[str, Any] | None = None,
) -> None:
    """Public wrapper: append a colored Review Results sheet to `wb`, named
    `sheet_name`. Used by the multi-sheet workbook flow in cli.py to build
    the consolidated `<input>_evaluated.xlsx` — one tab per doc, each tab is
    that doc's lab-layout review sheet with the same coloring/comments as
    the per-doc mismatches.xlsx.

    Hides the openpyxl import boilerplate from callers and pins
    `position=None` so the sheets append in iteration order (no fighting
    over position 0).
    """
    import openpyxl  # noqa: WPS433
    from openpyxl.styles import Alignment, Font, PatternFill  # noqa: WPS433
    from openpyxl.comments import Comment  # noqa: WPS433

    _write_review_results_sheet(
        wb, doc_id, cell_verdicts,
        openpyxl, PatternFill, Font, Alignment, Comment,
        field_map=field_map, sheet_name=sheet_name, position=None,
    )


def _verdicts_to_tsv(doc_id: str, verdicts: list[dict]) -> str:
    out = ["doc_id\tsection\tidentity\tcolumn\tverdict\tgt\tgt_canon\textracted\tex_canon"]
    for v in verdicts:
        if v["verdict"] in (WRONG, FN, FP):
            out.append("\t".join([
                doc_id, str(v.get("section")),
                " / ".join(str(x) for x in v.get("identity") or ()),
                str(v.get("column")), v["verdict"],
                str(v.get("gt") or ""), str(v.get("gt_canon") or ""),
                str(v.get("extracted") or ""), str(v.get("ex_canon") or ""),
            ]))
    return "\n".join(out)


def _write_summary_md(path: Path, doc_id: str, metrics: dict[str, Any]) -> None:
    o = metrics.get("overall", {})
    row_lvl = metrics.get("row_level", {})
    fields = metrics.get("field_level", {})

    # Worst-performing fields (by F1 ascending; only fields with non-zero support)
    field_rows = []
    for h, m in fields.items():
        support = m.get("tp", 0) + m.get("fn", 0) + m.get("wrong", 0)
        if support == 0:
            continue
        field_rows.append((h, m["f1"], m["precision"], m["recall"], support))
    field_rows.sort(key=lambda r: r[1])

    lines: list[str] = []
    lines.append(f"# Evaluation Summary — {doc_id}\n")
    lines.append(f"**Overall F1: {o.get('f1', 0):.3f}**   "
                 f"(precision {o.get('precision', 0):.3f}, recall {o.get('recall', 0):.3f})\n")
    lines.append("## Row-level\n")
    lines.append("| Section    | GT | Extracted | Matched | Missing | Spurious | Precision | Recall | F1 |")
    lines.append("|------------|---:|----------:|--------:|--------:|---------:|----------:|-------:|----:|")
    for sec in ("variants", "biomarkers", "overall"):
        rm = row_lvl.get(sec) or {}
        lines.append(f"| {sec:<11}| {rm.get('gt',0)} | {rm.get('extracted',0)} | "
                     f"{rm.get('matched',0)} | {rm.get('missing',0)} | {rm.get('spurious',0)} | "
                     f"{rm.get('precision',0):.3f} | {rm.get('recall',0):.3f} | {rm.get('f1',0):.3f} |")
    lines.append("\n## Worst-performing fields (by F1)\n")
    lines.append("| Field | F1 | Precision | Recall | Support |")
    lines.append("|-------|---:|----------:|-------:|--------:|")
    for h, f1, p, r, sup in field_rows[:8]:
        lines.append(f"| {h} | {f1:.3f} | {p:.3f} | {r:.3f} | {sup} |")
    lines.append("\n## Best-performing fields (by F1)\n")
    lines.append("| Field | F1 | Support |")
    lines.append("|-------|---:|--------:|")
    for h, f1, _, _, sup in sorted(field_rows, key=lambda r: -r[1])[:8]:
        lines.append(f"| {h} | {f1:.3f} | {sup} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
