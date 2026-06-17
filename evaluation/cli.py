"""CLI: `python -m evaluation --doc <doc_id>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Iterable

import yaml

from evaluation.lib.load_extraction import load_extraction
from evaluation.lib.load_ground_truth import load_ground_truth
from evaluation.lib.match_rows import match
from evaluation.lib.metrics import compute
from evaluation.lib.render_report import write_all

logger = logging.getLogger("evaluation")


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


def _eval_one(doc_id: str, gt_path: Path, ex_path: Path, out_dir: Path) -> int:
    if not gt_path.is_file():
        logger.error("ground-truth file not found: %s", gt_path)
        return 2
    if not ex_path.is_file():
        logger.error("extraction file not found: %s", ex_path)
        return 2

    field_map = _load_field_map()
    gt_rows = load_ground_truth(gt_path, field_map)
    ex_rows = load_extraction(ex_path, field_map)

    logger.info("evaluation[%s]: gt_rows=%d  ex_rows=%d", doc_id, len(gt_rows), len(ex_rows))
    pairs = match(gt_rows, ex_rows)
    metrics = compute(pairs, field_map.get("columns") or [])
    write_all(out_dir, doc_id, gt_path, ex_path, metrics)
    logger.info("evaluation[%s]: wrote %s", doc_id, out_dir)
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evaluation",
                                 description="Evaluate extraction_production.json against SME ground-truth XLSX.")
    ap.add_argument("--doc", help="doc_id (e.g. full_report_Redacted). Required unless --batch.")
    ap.add_argument("--gt", type=Path, default=None,
                    help="Override path to GT xlsx (default: ground_truth/<doc>.xlsx)")
    ap.add_argument("--extraction", type=Path, default=None,
                    help="Override path to extraction_production.json")
    ap.add_argument("--out", type=Path, default=None,
                    help="Override output dir (default: local_runs/evaluations/<doc>/)")
    ap.add_argument("--batch", action="store_true",
                    help="Eval every doc with both GT.xlsx + extraction_production.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    if args.batch:
        rc = 0
        for doc_id in _discover_batch():
            gt, ex, out = _default_paths(doc_id)
            rc |= _eval_one(doc_id, gt, ex, out)
        return rc

    if not args.doc:
        ap.error("--doc is required (or use --batch)")

    gt, ex, out = _default_paths(args.doc)
    if args.gt:         gt = args.gt
    if args.extraction: ex = args.extraction
    if args.out:        out = args.out
    return _eval_one(args.doc, gt, ex, out)


if __name__ == "__main__":
    sys.exit(main())
