"""CLI: `python -m evaluation --doc <doc_id>`."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

import yaml

from evaluation.lib.load_extraction import load_extraction
from evaluation.lib.load_ground_truth import load_ground_truth
from evaluation.lib.match_rows import match
from evaluation.lib.metrics import compute
from evaluation.lib.render_report import write_all, write_review_results_into

logger = logging.getLogger("evaluation")


# ─────────────────────────────────────────────────────────────────────────────
# Doc-id resolution — substring match against local_runs/artifacts/ folders
# ─────────────────────────────────────────────────────────────────────────────


class ResolveStatus(str, Enum):
    OK = "ok"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"


@dataclass
class ResolveResult:
    """Outcome of resolving a sheet-name / --doc identifier against the
    `local_runs/artifacts/` directory.

    - `status=OK`        → `doc_id` is the canonical folder name to use.
    - `status=NO_MATCH`  → no artifact folder substring-matched the input.
    - `status=AMBIGUOUS` → multiple folders matched; the caller should refuse
                            and surface the candidate list to the SME.
    """
    status: ResolveStatus
    doc_id: str | None = None
    candidates: list[str] = field(default_factory=list)
    detail: str = ""


# Extensions stripped from the user-supplied identifier before searching.
_STRIP_EXTS: tuple[str, ...] = (
    ".pdf", ".PDF",
    ".xlsx", ".XLSX",
    ".docx", ".DOCX",
)


def _strip_known_ext(name: str) -> str:
    """Strip ONE known doc extension from `name` if present, else return as-is."""
    s = name.strip()
    for ext in _STRIP_EXTS:
        if s.endswith(ext):
            return s[: -len(ext)]
    return s


def _resolve_doc_id(name: str, art_dir: Path) -> ResolveResult:
    """Substring-match `name` against every subdirectory of `art_dir`.

    Rules:
      • Strip one of the known doc extensions (`.pdf`, `.xlsx`, …) from the
        input first so sheet names like "full_report_Redacted.pdf" search
        for the substring "full_report_Redacted".
      • Match is case-insensitive — the SME may have any combination of
        casing in their sheet name vs. the actual artifact folder.
      • Only directories under `art_dir` are considered candidates.
      • A folder is a valid match only if it ALSO contains an
        `extraction_production.json` — a half-built artifact folder with
        no extraction is treated as if the folder didn't exist.

    On AMBIGUOUS, every collision (even those missing the extraction file)
    is reported so the SME can see exactly which folders are colliding.
    """
    if not art_dir.is_dir():
        return ResolveResult(
            status=ResolveStatus.NO_MATCH,
            detail=f"artifacts dir does not exist: {art_dir}",
        )

    cleaned = _strip_known_ext(name)
    if not cleaned:
        return ResolveResult(
            status=ResolveStatus.NO_MATCH,
            detail="empty name after stripping known extension",
        )

    needle = cleaned.lower()
    matches: list[str] = []
    for sub in sorted(art_dir.iterdir()):
        if sub.is_dir() and needle in sub.name.lower():
            matches.append(sub.name)

    if len(matches) == 0:
        return ResolveResult(
            status=ResolveStatus.NO_MATCH,
            detail=f"no artifact folder contained substring {cleaned!r}",
        )
    if len(matches) > 1:
        # AMBIGUOUS — show every collision so the SME can rename or qualify.
        return ResolveResult(
            status=ResolveStatus.AMBIGUOUS,
            candidates=matches,
            detail=(f"substring {cleaned!r} matched {len(matches)} folders: "
                    + ", ".join(matches)),
        )

    chosen = matches[0]
    if not (art_dir / chosen / "extraction_production.json").is_file():
        return ResolveResult(
            status=ResolveStatus.NO_MATCH,
            detail=(f"matched folder {chosen!r} has no "
                    f"extraction_production.json — pipeline may not have run"),
        )

    return ResolveResult(status=ResolveStatus.OK, doc_id=chosen, candidates=[chosen])


def _repo_root() -> Path:
    """The repo root, derived from this file's location (evaluation/cli.py)."""
    return Path(__file__).resolve().parent.parent


def _default_paths(doc_id: str) -> tuple[Path, Path, Path]:
    """Convention paths (all relative to repo root)."""
    root = _repo_root()
    gt   = root / "ground_truth"   / f"{doc_id}.xlsx"
    ex   = root / "local_runs" / "artifacts"   / doc_id / "extraction_production.json"
    out  = root / "local_runs" / "evaluations" / doc_id
    return gt, ex, out


def _load_field_map() -> dict:
    p = _repo_root() / "evaluation" / "config" / "field_map.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _eval_one(
    doc_id: str,
    gt_path: Path,
    ex_path: Path,
    out_dir: Path,
    *,
    sheet_name: str | None = None,
    file_prefix: str = "",
    consolidated_wb=None,
    consolidated_sheet_name: str | None = None,
) -> int:
    """Run one eval. Optionally also append this doc's colored Review
    Results sheet to a shared `consolidated_wb` (the multi-sheet workbook
    flow uses this to build `<input>_evaluated.xlsx`)."""
    if not gt_path.is_file():
        logger.error("ground-truth file not found: %s", gt_path)
        return 2
    if not ex_path.is_file():
        logger.error("extraction file not found: %s", ex_path)
        return 2

    field_map = _load_field_map()
    gt_rows = load_ground_truth(gt_path, field_map, sheet_name=sheet_name)
    ex_rows = load_extraction(ex_path, field_map)

    logger.info("evaluation[%s]: gt_rows=%d  ex_rows=%d", doc_id, len(gt_rows), len(ex_rows))
    pairs = match(gt_rows, ex_rows)
    metrics = compute(pairs, field_map.get("columns") or [])

    # Peek the per-cell verdicts BEFORE write_all pops them — we may need
    # them again to render the consolidated workbook below.
    cell_verdicts_copy = list(metrics.get("_cell_verdicts", []) or [])

    write_all(out_dir, doc_id, gt_path, ex_path, metrics,
              field_map=field_map, file_prefix=file_prefix)
    logger.info("evaluation[%s]: wrote %s (prefix=%r)", doc_id, out_dir, file_prefix)

    # If the caller is also building a consolidated workbook, append this
    # doc's lab-layout review sheet to it. Same rendering function as the
    # per-doc mismatches.xlsx — same colors, same comments, same column
    # order — just under the doc filename as a tab inside the shared wb.
    if consolidated_wb is not None and cell_verdicts_copy:
        write_review_results_into(
            consolidated_wb, doc_id, cell_verdicts_copy,
            consolidated_sheet_name or doc_id,
            field_map=field_map,
        )

    return 0


def _discover_batch() -> Iterable[str]:
    """Doc IDs that have BOTH a GT.xlsx and an extraction_production.json."""
    root = _repo_root()
    gt_dir = root / "ground_truth"
    art_dir = root / "local_runs" / "artifacts"
    if not (gt_dir.is_dir() and art_dir.is_dir()):
        return []
    seen: set[str] = set()
    for gt_xlsx in gt_dir.glob("*.xlsx"):
        doc_id = gt_xlsx.stem
        if (art_dir / doc_id / "extraction_production.json").is_file():
            seen.add(doc_id)
    return sorted(seen)


# ─────────────────────────────────────────────────────────────────────────────
# Multi-sheet workbook mode — one workbook in, per-doc outputs into artifacts/
# ─────────────────────────────────────────────────────────────────────────────


def _write_errors_xlsx(out_path: Path, input_name: str, errors: list[dict]) -> None:
    """Write a one-sheet error report listing every input-sheet that couldn't
    be paired with an artifact folder. Only called when len(errors) > 0.

    Sheet columns:
      Sheet name | Attempted match key | Error type | Detail | Candidates
    """
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        # Degrade to a TSV next to where the XLSX would have gone so we still
        # surface the failures somewhere readable.
        lines = ["sheet_name\tattempted_key\terror_type\tdetail\tcandidates"]
        for e in errors:
            lines.append("\t".join([
                str(e.get("sheet_name", "")),
                str(e.get("attempted_key", "")),
                str(e.get("error_type", "")),
                str(e.get("detail", "")),
                str(e.get("candidates", "")),
            ]))
        out_path.with_suffix(".tsv").write_text("\n".join(lines), encoding="utf-8")
        return

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Errors"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
    header_align = Alignment(horizontal="left", vertical="center", wrap_text=True)
    headers = ["Sheet name", "Attempted match key", "Error type", "Detail", "Candidates"]
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=col_idx, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = header_align
        col_letter = openpyxl.utils.get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = max(14, min(60, len(h) + 14))
    ws.freeze_panes = "A2"

    for r_idx, e in enumerate(errors, start=2):
        ws.cell(row=r_idx, column=1, value=e.get("sheet_name", ""))
        ws.cell(row=r_idx, column=2, value=e.get("attempted_key", ""))
        ws.cell(row=r_idx, column=3, value=e.get("error_type", ""))
        ws.cell(row=r_idx, column=4, value=e.get("detail", ""))
        ws.cell(row=r_idx, column=5, value=e.get("candidates", ""))

    # Note the input workbook name in cell A<last+2> for context.
    ws.cell(row=len(errors) + 3, column=1,
            value=f"(source workbook: {input_name})").font = Font(italic=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))


def _eval_workbook(workbook_path: Path) -> int:
    """For each sheet in `workbook_path`, treat the sheet name as a SUBSTRING
    search key against `local_runs/artifacts/`, find the (unique) artifact
    folder whose name contains the substring, and run the eval against that
    folder's `extraction_production.json`. Per-doc outputs land in
    `local_runs/artifacts/<folder>/eval_*` (existing behavior).

    Failure modes are SOFT — the run continues and an error row is recorded:
      • No artifact folder contains the substring → NO_MATCH
      • Multiple folders contain the substring → AMBIGUOUS (refused to guess)
      • The matched folder is missing extraction_production.json → NO_MATCH

    After the loop:
      • Consolidated colored workbook `<input>_evaluated.xlsx` is written to
        `local_runs/evaluations/` with one sheet per successfully evaluated
        doc (existing behavior).
      • If any sheets were skipped due to resolution errors, an additional
        `<input>_errors.xlsx` is written to the same dir, listing every
        skipped sheet with the reason and candidate folders (when ambiguous).
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise SystemExit("openpyxl is required: pip install openpyxl") from exc

    if not workbook_path.is_file():
        logger.error("workbook not found: %s", workbook_path)
        return 2

    wb = openpyxl.load_workbook(str(workbook_path), data_only=True, read_only=True)
    sheet_names = list(wb.sheetnames)
    logger.info("workbook[%s]: %d sheets — %s", workbook_path.name,
                len(sheet_names), sheet_names)

    root = _repo_root()
    art_dir = root / "local_runs" / "artifacts"

    # Consolidated workbook — accumulated across all docs in this batch.
    consolidated_wb = openpyxl.Workbook()

    rc = 0
    n_ran = 0
    n_skipped = 0
    errors: list[dict] = []   # collected for the error-report XLSX

    for sheet in sheet_names:
        res = _resolve_doc_id(sheet, art_dir)
        attempted_key = _strip_known_ext(sheet)

        if res.status is ResolveStatus.AMBIGUOUS:
            logger.error(
                "sheet %r: AMBIGUOUS — substring %r matched %d folders: %s",
                sheet, attempted_key, len(res.candidates),
                ", ".join(res.candidates))
            errors.append({
                "sheet_name": sheet,
                "attempted_key": attempted_key,
                "error_type": "AMBIGUOUS",
                "detail": res.detail,
                "candidates": ", ".join(res.candidates),
            })
            n_skipped += 1
            continue

        if res.status is ResolveStatus.NO_MATCH:
            logger.warning("sheet %r: NO_MATCH — %s", sheet, res.detail)
            errors.append({
                "sheet_name": sheet,
                "attempted_key": attempted_key,
                "error_type": "NO_MATCH",
                "detail": res.detail,
                "candidates": "",
            })
            n_skipped += 1
            continue

        # OK — run the eval on the resolved folder.
        doc_id = res.doc_id  # canonical folder name
        assert doc_id is not None
        ex_path = art_dir / doc_id / "extraction_production.json"
        out_dir = art_dir / doc_id
        logger.info("sheet %r → resolved to artifact folder %r", sheet, doc_id)

        single_rc = _eval_one(
            doc_id, workbook_path, ex_path, out_dir,
            sheet_name=sheet, file_prefix="eval_",
            consolidated_wb=consolidated_wb,
            consolidated_sheet_name=sheet,   # keep the user's original sheet name
        )
        rc |= single_rc
        n_ran += 1

    # Drop the default empty "Sheet" openpyxl created, but only if we wrote
    # at least one real sheet (else save would fail on a zero-sheet wb).
    default_sheet = "Sheet"
    if default_sheet in consolidated_wb.sheetnames and len(consolidated_wb.sheetnames) > 1:
        del consolidated_wb[default_sheet]

    # Save the consolidated workbook at local_runs/evaluations/<input>_evaluated.xlsx
    evals_dir = root / "local_runs" / "evaluations"
    evals_dir.mkdir(parents=True, exist_ok=True)
    out_xlsx = evals_dir / f"{workbook_path.stem}_evaluated.xlsx"
    if n_ran == 0:
        logger.warning(
            "consolidated workbook NOT written: no doc sheets resolved to an "
            "artifact folder (intended path: %s)", out_xlsx)
    else:
        consolidated_wb.save(str(out_xlsx))
        logger.info("consolidated workbook: wrote %s (%d sheet(s))",
                    out_xlsx, len(consolidated_wb.sheetnames))

    # Write the error report when we have anything to report.
    if errors:
        err_xlsx = evals_dir / f"{workbook_path.stem}_errors.xlsx"
        _write_errors_xlsx(err_xlsx, workbook_path.name, errors)
        logger.warning("error report: wrote %s (%d row(s))", err_xlsx, len(errors))

    logger.info("workbook done: ran=%d  skipped=%d  errors=%d  rc=%d",
                n_ran, n_skipped, len(errors), rc)
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m evaluation",
        description=(
            "Evaluate extraction_production.json against SME ground-truth XLSX. "
            "Three modes: (1) --doc / --batch reads a single-sheet GT per doc "
            "from ground_truth/<doc>.xlsx and writes to local_runs/evaluations/. "
            "(2) --workbook reads a multi-sheet GT workbook (sheet name = doc "
            "filename) and writes per-doc outputs into local_runs/artifacts/<doc>/ "
            "with an 'eval_' filename prefix so they coexist with pipeline "
            "artifacts (extraction_production.json, link_metrics.json, etc)."
        ),
    )
    ap.add_argument("--doc", help="doc_id (e.g. full_report_Redacted). Required unless --batch or --workbook.")
    ap.add_argument("--gt", type=Path, default=None,
                    help="Override path to GT xlsx (default: ground_truth/<doc>.xlsx)")
    ap.add_argument("--extraction", type=Path, default=None,
                    help="Override path to extraction_production.json")
    ap.add_argument("--out", type=Path, default=None,
                    help="Override output dir (default: local_runs/evaluations/<doc>/)")
    ap.add_argument("--batch", action="store_true",
                    help="Eval every doc with both GT.xlsx + extraction_production.json")
    ap.add_argument("--workbook", type=Path, default=None,
                    help=("Multi-sheet GT workbook. Each sheet name = doc filename "
                          "(extension stripped). Outputs land in "
                          "local_runs/artifacts/<doc>/ with 'eval_' prefix."))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    if args.workbook:
        return _eval_workbook(args.workbook)

    if args.batch:
        rc = 0
        for doc_id in _discover_batch():
            gt, ex, out = _default_paths(doc_id)
            rc |= _eval_one(doc_id, gt, ex, out)
        return rc

    if not args.doc:
        ap.error("--doc is required (or use --batch / --workbook)")

    # Substring-resolve --doc against artifact folders (same logic as --workbook
    # sheet matching). This lets `--doc 06CDGMFM97SR` resolve to a longer
    # folder name like `2025-12-09_..._06CDGMFM97SR_redacted` without typing it.
    # Overrides (--extraction / --out) skip resolution since the user is
    # pointing at exact paths.
    root = _repo_root()
    art_dir = root / "local_runs" / "artifacts"
    if args.extraction is None and args.out is None:
        res = _resolve_doc_id(args.doc, art_dir)
        if res.status is ResolveStatus.AMBIGUOUS:
            logger.error(
                "--doc %r is AMBIGUOUS — substring matched %d folders: %s. "
                "Use a more specific identifier or pass --extraction / --out "
                "directly.",
                args.doc, len(res.candidates), ", ".join(res.candidates))
            return 2
        if res.status is ResolveStatus.NO_MATCH:
            logger.error("--doc %r did not match any artifact folder: %s",
                         args.doc, res.detail)
            return 2
        # OK — swap the input doc_id for the canonical folder name.
        if res.doc_id and res.doc_id != args.doc:
            logger.info("--doc %r → resolved to artifact folder %r",
                        args.doc, res.doc_id)
            args.doc = res.doc_id

    gt, ex, out = _default_paths(args.doc)
    if args.gt:         gt = args.gt
    if args.extraction: ex = args.extraction
    if args.out:        out = args.out
    return _eval_one(args.doc, gt, ex, out)


if __name__ == "__main__":
    sys.exit(main())
