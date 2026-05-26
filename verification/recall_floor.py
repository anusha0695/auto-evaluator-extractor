"""
RecallFloorVerifier (P3-M3) — block-role recall floor for the narrative sections.

Deterministic detector: if the Block Profiler tagged a block with a `role` whose
mapped schema `field` came back EMPTY in the extraction, that's a CANDIDATE miss.
An injected `reread_fn` then confirms "genuinely absent" (drop — true negative)
vs "present, we missed it" (real miss); determinations are memoized in a shared
`block_reads` cache so no block is re-read twice (and it becomes audit provenance).

ADVISORY-FIRST (M3): the emitted scorecard is `passed=True` and lists the
candidate/confirmed misses (with `strictness` loud|quiet) in `field_errors`. The
ping-back loop (M6) consumes the `loud` ones; the router weighs the rest. So this
milestone surfaces the signal without yet changing routing.

Mapping is `config/recall_floor.yaml` (schema-owned field side); a completeness
check validates every rule references a real schema section/field.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _load_rules(path: str) -> list[dict[str, Any]]:
    import yaml
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return list(data.get("role_field_rules") or [])


def _nonempty(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (str,)):
        return v.strip() != ""
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


def _walk(obj: Any, path: list[str]) -> Any:
    cur = obj
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _field_present(envelope: dict[str, Any], rule: dict[str, Any]) -> bool:
    """True if the rule's mapped field is populated in the envelope."""
    section = rule.get("section")
    check = rule.get("check")
    path = rule.get("path") or []
    sec = envelope.get(section) or {}
    if check in ("scalar", "scalar_or_array"):
        return _nonempty(_walk(sec, path))
    if check in ("specimen_field", "specimen_array"):
        specimens = (envelope.get("significant_findings") or {}).get("specimen_findings") or []
        for sp in specimens:
            if not isinstance(sp, dict):
                continue
            val = _walk(sp, path)
            if check == "specimen_array":
                if isinstance(val, list) and len(val) > 0:
                    return True
            elif _nonempty(val):
                return True
        return False
    # unknown check kind → treat as present (don't false-flag)
    return True


class RecallFloorVerifier:
    """Block-role recall floor. `reread_fn(block_id, section, field_path) -> bool`
    returns True if the field IS genuinely present in that block (so the miss is
    a true negative and should be dropped). If not injected, candidate misses are
    reported UNCONFIRMED (never silently passed)."""

    def __init__(self, *, mapping_path: str = "config/recall_floor.yaml",
                 reread_fn: Callable[..., bool] | None = None) -> None:
        self._rules = _load_rules(mapping_path)
        self._reread = reread_fn

    def verify(self, *, envelope: dict[str, Any], block_profiles: list[dict[str, Any]],
               block_reads: dict[str, Any] | None = None) -> dict[str, Any]:
        block_reads = block_reads if block_reads is not None else {}
        # role -> first block_id carrying it (for citation + re-read)
        role_block: dict[str, str] = {}
        for bp in (block_profiles or []):
            if isinstance(bp, dict) and bp.get("text_role") and bp.get("text_role") not in role_block:
                role_block[bp["text_role"]] = bp.get("block_id")

        misses: list[dict[str, Any]] = []
        for rule in self._rules:
            role = rule.get("role")
            if role not in role_block:
                continue                       # this role isn't on the page → no expectation
            if _field_present(envelope, rule):
                continue                       # field populated → no miss
            block_id = role_block[role]
            field_label = f"{rule.get('section')}.{'.'.join(rule.get('path') or [])}"
            status = "unconfirmed"
            # confirm true-absence via the (memoized) re-read
            cache_key = f"{block_id}:{field_label}"
            if cache_key in block_reads:
                determination = block_reads[cache_key].get("determination")
            elif self._reread is not None:
                present = bool(self._reread(block_id=block_id, section=rule.get("section"),
                                            field_path=rule.get("path") or []))
                determination = "present" if present else "absent"
                block_reads[cache_key] = {"determination": determination,
                                          "decided_by": "recall_floor.reread", "field": field_label}
            else:
                determination = None
            if determination == "absent":
                continue                       # confirmed true negative → not a miss
            status = "confirmed_miss" if determination == "present" else "unconfirmed"
            misses.append({
                "role": role, "field_name": field_label, "block_id": block_id,
                "strictness": rule.get("strictness", "quiet"), "status": status,
            })

        loud = [m for m in misses if m["strictness"] == "loud"]
        notes = (f"{len(misses)} recall-floor candidate miss(es) "
                 f"({len(loud)} loud); advisory — M6 loop consumes loud ones."
                 if misses else "no recall-floor misses")
        return {
            "verifier_name": "recall_floor",
            "passed": True,                    # advisory-first: never hard-fails routing yet
            "field_errors": misses[:50],
            "notes": notes,
        }


def validate_against_schema(mapping_path: str, schema_loader: Any) -> list[str]:
    """Completeness check: every rule references a real schema section, and (for
    scalar checks) a real top-level field of that section; specimen_* rules check
    the path head against the significant_findings specimen item. Returns issues."""
    rules = _load_rules(mapping_path)
    sections = set(schema_loader.list_sections())
    issues: list[str] = []
    # specimen item properties (for specimen_* checks)
    sf = schema_loader.get_section("significant_findings").raw_json_schema
    sp_item = (((sf.get("properties") or {}).get("specimen_findings") or {}).get("items") or {})
    sp_props = set((sp_item.get("properties") or {}).keys())
    for r in rules:
        sec = r.get("section")
        if sec not in sections:
            issues.append(f"rule role={r.get('role')} → unknown section {sec!r}")
            continue
        path = r.get("path") or []
        head = path[0] if path else None
        if r.get("check") in ("scalar", "scalar_or_array"):
            props = set((schema_loader.get_section(sec).raw_json_schema.get("properties") or {}).keys())
            if head not in props:
                issues.append(f"rule role={r.get('role')} → {sec}.{head} not a schema field")
        elif r.get("check") in ("specimen_field", "specimen_array"):
            if head not in sp_props:
                issues.append(f"rule role={r.get('role')} → significant_findings.specimen.{head} not a schema field")
    return issues
