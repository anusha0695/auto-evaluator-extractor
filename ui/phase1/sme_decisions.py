"""
SME decision model + apply logic (P3-M8d, pure core).

`build_review_model(doc_id, load)` assembles everything the review screen needs:
the escalation queue (sorted proposals-first → unresolved → rest), each item
enriched with its field-trace timeline + plain/technical explanation.

`apply_sme_decision(extraction, ...)` is the write-back. It NEVER mutates the
original — it deep-copies, applies the SME's choice by ref (approve → VMAW's
proposed value; edit → the SME's value; keep_flagged → no value), clears
`needs_review`, and stamps an `sme_review` audit marker. Verbatim `occurrences`
are never touched. Returns `(reviewed_extraction, decision_record)`. The caller
persists the reviewed copy to `extraction_reviewed.json` and appends the record to
`sme_decisions.json` (both local-only) via the IO helpers here.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from ui.phase1.evidence import field_rationale_map
from ui.phase1.field_trace import assemble_field_trace, explain

_REF_RE = re.compile(r"([A-Za-z_]+)|\[(\d+)\]")


# -- review model -----------------------------------------------------------


def build_review_model(doc_id: str, *, load: Callable[[str, str], Any]) -> dict[str, Any]:
    """Assemble the SME review model for a doc. `load(doc_id, kind)` returns the
    parsed artifact (inject `data_layer.load_artifact` in the app, a fake in tests).

    Banding: prefers the new `escalation_queue_banded` artifact (4 SME-priority
    bands — judgment, unresolved, review_light, drop_audit). Falls back to the
    legacy `escalation_queue` artifact and bands client-side, so older runs still
    render correctly."""
    # Prefer the persisted banded artifact; fall back to legacy queue + band here.
    banded = load(doc_id, "escalation_queue_banded")
    queue = list(load(doc_id, "escalation_queue") or [])
    if not banded:
        try:
            from pipeline.vmaw import band_escalation_queue
            banded = band_escalation_queue(queue)
        except Exception:
            banded = None

    agent_trace = load(doc_id, "agent_trace") or []
    repair_log = load(doc_id, "repair_log") or []
    vmaw_log = load(doc_id, "vmaw_log") or []
    binding_items = load(doc_id, "binding_items") or []
    verification = load(doc_id, "verification_v2")
    scorecards = verification.get("scorecards") if isinstance(verification, dict) else []
    extraction = load(doc_id, "extraction_reviewed") or load(doc_id, "extraction_v2") or {}
    rationales = field_rationale_map(extraction if isinstance(extraction, dict) else {})

    def _enrich(it: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(it)
        enriched["_trace"] = assemble_field_trace(
            ref=it.get("ref"), section=it.get("section"),
            agent_trace=agent_trace, scorecards=scorecards or [],
            repair_log=repair_log, vmaw_log=vmaw_log,
            binding_items=binding_items, field_rationale=rationales.get(it.get("ref"), ""))
        enriched["_plain"] = explain(it, mode="plain")
        enriched["_technical"] = explain(it, mode="technical")
        return enriched

    # Flat items list (legacy callers still get this). Ordered by band priority,
    # within band by VMAW proposal-readiness.
    def rank(it: dict[str, Any]) -> int:
        if it.get("vmaw_proposal"):
            return 0
        if it.get("vmaw_note"):
            return 1
        return 2

    # Per-band enriched lists (new shape callers should prefer).
    bands_out: dict[str, list[dict[str, Any]]] = {
        "judgment": [], "unresolved": [], "review_light": [], "drop_audit": []}
    if banded and isinstance(banded, dict) and "bands" in banded:
        for band_name in ("judgment", "unresolved", "review_light", "drop_audit"):
            for it in sorted(banded["bands"].get(band_name, []), key=rank):
                bands_out[band_name].append(_enrich(it))
    items_flat = (bands_out["judgment"] + bands_out["unresolved"]
                  + bands_out["review_light"] + bands_out["drop_audit"])

    band_counts = {b: len(bands_out[b]) for b in bands_out}
    counts = {
        # legacy counters (kept for back-compat)
        "queue": len(items_flat),
        "proposals": sum(1 for it in items_flat if it.get("vmaw_proposal")),
        "unresolved": band_counts["unresolved"],
        "auto_applied": sum(1 for v in vmaw_log
                            if (v.get("resolution") or {}).get("status") == "auto_applied"),
        # new banding KPIs
        "judgment":        band_counts["judgment"],
        "review_light":    band_counts["review_light"],
        "drop_audit":      band_counts["drop_audit"],
        "blocks_workflow": band_counts["judgment"] + band_counts["unresolved"],
    }
    return {"doc_id": doc_id, "items": items_flat, "bands": bands_out, "counts": counts}


# -- decision apply ---------------------------------------------------------


def apply_sme_decision(
    extraction: dict[str, Any],
    *,
    ref: str | None,
    action: str,                          # approve | edit | keep_flagged
    value: Any = None,                    # for edit
    proposal_value: Any = None,           # for approve
    reviewer: str = "sme",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one SME decision to a COPY of the extraction. Original untouched;
    occurrences never destroyed. Returns (reviewed_extraction, decision_record)."""
    reviewed = copy.deepcopy(extraction or {})
    node, key = _resolve_parent(reviewed, ref)
    target = _get(node, key)
    before = _value_snapshot(target)

    applied_value = proposal_value if action == "approve" else (value if action == "edit" else None)

    if action in ("approve", "edit"):
        if isinstance(target, dict):
            target["needs_review"] = False
            target["sme_review"] = {"action": action, "value": applied_value,
                                    "reviewer": reviewer, "confirmed": True}
        elif node is not None and key is not None:
            _set(node, key, applied_value)        # scalar field → set directly
    elif action == "keep_flagged" and isinstance(target, dict):
        target["sme_review"] = {"action": "keep_flagged", "reviewer": reviewer}

    decision = {
        "ref": ref, "action": action, "applied_value": applied_value,
        "before": before, "reviewer": reviewer,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    return reviewed, decision


# -- local-only persistence (PHI-safe path; never pushed) -------------------


def write_reviewed_extraction(doc_id: str, reviewed: dict[str, Any]) -> str:
    from ui.phase1.data_layer import _local_dir
    d = _local_dir() / "artifacts" / doc_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / "extraction_reviewed.json"
    path.write_text(json.dumps(reviewed, indent=2, default=str), encoding="utf-8")
    return str(path)


def append_sme_decision(doc_id: str, decision: dict[str, Any]) -> str:
    from ui.phase1.data_layer import _local_dir
    d = _local_dir() / "artifacts" / doc_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / "sme_decisions.json"
    existing: list[Any] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) or []
        except Exception:
            existing = []
    existing.append(decision)
    path.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
    return str(path)


# -- ref helpers ------------------------------------------------------------


def _tokens(ref: str | None) -> list[Any]:
    if not ref:
        return []
    out: list[Any] = []
    for name, idx in _REF_RE.findall(ref):
        if name:
            out.append(name)
        elif idx != "":
            out.append(int(idx))
    return out


def _resolve_parent(root: dict[str, Any], ref: str | None):
    toks = _tokens(ref)
    if not toks:
        return None, None
    node: Any = root
    for t in toks[:-1]:
        try:
            node = node[t]
        except (KeyError, IndexError, TypeError):
            return None, None
    return node, toks[-1]


def _get(node: Any, key: Any) -> Any:
    try:
        return node[key]
    except (KeyError, IndexError, TypeError):
        return None


def _set(node: Any, key: Any, value: Any) -> None:
    try:
        node[key] = value
    except (KeyError, IndexError, TypeError):
        pass


def _value_snapshot(target: Any) -> Any:
    """A small, occurrence-free before-image for the decision audit."""
    if isinstance(target, dict):
        return {k: v for k, v in target.items() if k != "occurrences"}
    return target
