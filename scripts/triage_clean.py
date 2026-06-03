"""
Backfill cleaner for `escalation_queue.json` after VMAW autonomy fix (Patch 1).

The patch teaches VMAW to DROP refute-class items (binding_refuted / link_cannot_form)
when VMAW investigated and returned an ungrounded + uncontested + non-empty rationale
— i.e. when VMAW agrees the binding/link can't form. Existing artifacts were written
BEFORE the patch and still carry those items under `triage_band: judgment`, even
though the VMAW proposal already says "no link can be formed."

This script replays that classification on a saved artifact: any queue item whose
kind is in {binding_refuted, link_cannot_form} AND whose vmaw_proposal is
ungrounded + uncontested + rationale-present is removed from the queue and logged.
Re-band via `pipeline.vmaw.band_escalation_queue` so the UI reflects the cleaned set.

Usage:
    PYTHONPATH=. python scripts/triage_clean.py --doc <doc_id>
    PYTHONPATH=. python scripts/triage_clean.py --doc demo --dry-run
    PYTHONPATH=. python scripts/triage_clean.py --doc demo --queue-name escalation_queue
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ARTIFACTS = _ROOT / "local_runs" / "artifacts"

_REFUTE_KINDS = {"binding_refuted", "link_cannot_form"}


def _is_vmaw_refutation(item: dict) -> bool:
    """Match the Patch-1 condition: refute-class kind with an ungrounded +
    uncontested + non-empty-rationale VMAW proposal."""
    if item.get("kind") not in _REFUTE_KINDS:
        return False
    prop = item.get("vmaw_proposal")
    if not isinstance(prop, dict):
        return False
    if prop.get("grounded") is True:
        return False
    if prop.get("contested") is True:
        return False
    rationale = str(prop.get("rationale") or "").strip()
    return bool(rationale)


def _load_json(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARN: failed to read {path.name}: {exc}", file=sys.stderr)
        return None


def _atomic_write(path: Path, payload: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True, help="doc_id (artifact folder name)")
    ap.add_argument("--queue-name", default="escalation_queue",
                    help="base name (no .json) of the queue artifact; "
                         "also rebands `<name>_banded.json` if present")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be dropped; do not write")
    args = ap.parse_args(argv or sys.argv[1:])

    doc_dir = _ARTIFACTS / args.doc
    if not doc_dir.is_dir():
        print(f"ERROR: no artifacts at {doc_dir}", file=sys.stderr)
        return 1

    queue_path = doc_dir / f"{args.queue_name}.json"
    queue = _load_json(queue_path)
    if not isinstance(queue, list):
        print(f"ERROR: {queue_path.name} missing or not a list", file=sys.stderr)
        return 1

    kept: list[dict] = []
    dropped: list[dict] = []
    for item in queue:
        if isinstance(item, dict) and _is_vmaw_refutation(item):
            dropped.append(item)
            continue
        kept.append(item if isinstance(item, dict) else {"raw": item})

    print(f"Queue: {len(queue)} items → keep {len(kept)}, drop {len(dropped)}")
    for d in dropped[:10]:
        kind = d.get("kind"); ref = d.get("ref")
        rat = str((d.get("vmaw_proposal") or {}).get("rationale") or "").strip()
        print(f"  - drop {kind} @ {ref}  rationale: {rat[:90]}")
    if len(dropped) > 10:
        print(f"  … and {len(dropped) - 10} more")

    if args.dry_run:
        print("(--dry-run: no files written)")
        return 0

    # Save a backup before overwriting.
    backup = queue_path.with_suffix(".json.pre_triage_clean")
    if not backup.exists():
        backup.write_text(queue_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"backup: {backup.name}")

    # Mark each dropped item with a vmaw_note before logging — symmetric with the
    # Patch-1 path that produces `dropped_ungroundable` items for new runs.
    log_path = doc_dir / "triage_clean_drops.json"
    log = []
    if log_path.exists():
        loaded = _load_json(log_path)
        if isinstance(loaded, list):
            log = loaded
    for d in dropped:
        d2 = dict(d)
        d2["kind"] = "dropped_ungroundable"
        d2["vmaw_note"] = {
            "status": "vmaw_refuted",
            "reason": "Backfilled by triage_clean.py — VMAW rationale already refuted; "
                      "record removed from queue (envelope not modified by this script).",
            "rationale": str((d.get("vmaw_proposal") or {}).get("rationale") or "").strip(),
        }
        log.append(d2)
    _atomic_write(log_path, log)

    _atomic_write(queue_path, kept)
    print(f"wrote: {queue_path.name}  ({len(kept)} items)")
    print(f"audit: {log_path.name}    ({len(log)} historical drops total)")

    # Optional re-banding.
    banded_path = doc_dir / f"{args.queue_name}_banded.json"
    if banded_path.exists():
        try:
            from pipeline.vmaw import band_escalation_queue  # type: ignore
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: could not re-band (import failed): {exc}", file=sys.stderr)
        else:
            rebanded = band_escalation_queue(kept)
            _atomic_write(banded_path, rebanded)
            summary = rebanded.get("summary") if isinstance(rebanded, dict) else {}
            print(f"rebanded: {banded_path.name}  bands={summary}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
