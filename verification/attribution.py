"""
AttributionVerifier (P3-M10b) — owner-keyed attribution for every array-of-objects.

For each target in `config/attribution_map.yaml` it walks the object array and, for
every object, checks that each `attribute` actually describes that object's
`owner_key` entity in the source (e.g. is this `laterality` really specimen A's, or
did it bleed from specimen B?). This is the binding verifier's relationship idea
applied to object identity.

Anchoring:
  • owner_key present  → anchor on its value (e.g. specimen_id "A").
  • owner_key null/empty → anchor on POSITION (the [idx]); the missing key is NOT a
    defect here (recall_floor owns "expected id present but missing").

Autonomy (mirrors VMAW/binding): the semantic decision is the injected
`attribution_fn(...) -> verdict`; we only apply the deterministic GATE on its result:
  grounded  → pass (no error)            contested → escalate-class error (loud)
  ungrounded → error (quiet)             no hook   → "unverified" advisory (quiet)
Multiplicity (`n_owners` = sibling objects in the same array) is recorded so the
ping-back/escalation layer (M10c) can raise scrutiny when ≥2 owners contend; it does
NOT gate whether the check runs (locked decision: always check, single-owner too).

ADVISORY-FIRST (M10b): the scorecard is `passed=True`; it lists non-grounded
candidates in `field_errors` and a per-target tally in `metrics` (the M10d feed).
Routing of `contested` into the repair/VMAW/SME path is wired in M10c.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _load_targets(path: str) -> list[dict[str, Any]]:
    import yaml
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return list(data.get("attribution_targets") or [])


def _nonempty(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


def _subtree_blocks(obj: Any) -> list[str]:
    """Block ids from any `occurrences` under obj (citation for the attribution)."""
    out: list[str] = []

    def rec(o: Any) -> None:
        if isinstance(o, dict):
            for x in (o.get("occurrences") or []):
                if isinstance(x, dict) and x.get("block_id"):
                    out.append(str(x["block_id"]))
            for k, v in o.items():
                if k != "occurrences":
                    rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)

    rec(obj)
    return list(dict.fromkeys(out))


def _collect_objects(section_obj: Any, section: str, array_keys: list[str]):
    """Yield (ref, obj, n_siblings) for every object at the end of the array path.
    n_siblings = length of the LAST array (the object's immediate peer group)."""
    results: list[tuple[str, dict[str, Any], int]] = []

    def rec(node: Any, keys: list[str], ref: str) -> None:
        if not keys:
            return
        k = keys[0]
        arr = node.get(k) if isinstance(node, dict) else None
        if not isinstance(arr, list):
            return
        last = len(keys) == 1
        for idx, item in enumerate(arr):
            child_ref = f"{ref}.{k}[{idx}]"
            if last:
                if isinstance(item, dict):
                    results.append((child_ref, item, len(arr)))
            else:
                rec(item, keys[1:], child_ref)

    rec(section_obj, array_keys, section)
    return results


class AttributionVerifier:
    """Owner-keyed attribution. `attribution_fn(owner_key, owner_id, anchored_by,
    attribute, value, candidate_block_ids, n_owners) -> str` returns one of
    'grounded' | 'contested' | 'ungrounded'. Default None → advisory 'unverified'
    (never silently passed)."""

    def __init__(self, *, mapping_path: str = "config/attribution_map.yaml",
                 attribution_fn: Callable[..., str] | None = None) -> None:
        self._targets = _load_targets(mapping_path)
        self._fn = attribution_fn

    def verify(self, *, envelope: dict[str, Any],
               blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        metrics: dict[str, dict[str, int]] = {}
        text_by_id = {b.get("block_id"): (b.get("text") or "")
                      for b in (blocks or []) if isinstance(b, dict)}

        def _call_hook(**kw) -> str:
            """Call the semantic hook with candidate block TEXTS; fall back to the
            id-only signature for legacy/stub hooks (mirrors the linker's pattern)."""
            cand = [{"block_id": bid, "text": text_by_id.get(bid, "")}
                    for bid in (kw.get("candidate_block_ids") or [])]
            try:
                return str(self._fn(candidate_blocks=cand, **kw) or "ungrounded")
            except TypeError:
                return str(self._fn(**kw) or "ungrounded")

        for tgt in self._targets:
            name = tgt.get("name") or "?"
            section = tgt.get("section")
            owner_key = tgt.get("owner_key")
            attrs = list(tgt.get("attributes") or [])
            m = metrics.setdefault(name, {"checked": 0, "grounded": 0,
                                          "contested": 0, "ungrounded": 0, "unverified": 0})
            sec_obj = envelope.get(section) or {}
            for ref, obj, n_owners in _collect_objects(sec_obj, section, tgt.get("array_keys") or []):
                raw_owner = obj.get(owner_key)
                if _nonempty(raw_owner):
                    owner_id, anchored_by = str(raw_owner), "owner_key"
                else:
                    owner_id, anchored_by = ref.rsplit(".", 1)[-1], "position"  # the [idx]
                blocks_ids = _subtree_blocks(obj)
                for attr in attrs:
                    val = obj.get(attr)
                    if not _nonempty(val):
                        continue                       # nothing extracted → nothing to attribute
                    m["checked"] += 1
                    if self._fn is not None:
                        try:
                            verdict = _call_hook(
                                owner_key=owner_key, owner_id=owner_id, anchored_by=anchored_by,
                                attribute=attr, value=val, candidate_block_ids=blocks_ids,
                                n_owners=n_owners)
                        except Exception:  # noqa: BLE001 — a hook error degrades to advisory
                            logger.exception("attribution_fn failed for %s.%s", ref, attr)
                            verdict = "unverified"
                    else:
                        verdict = "unverified"
                    verdict = verdict if verdict in ("grounded", "contested", "ungrounded") else "unverified"
                    m[verdict] += 1
                    if verdict == "grounded":
                        continue                       # attribution confirmed → no error
                    errors.append({
                        "target": name, "ref": f"{ref}.{attr}", "owner_ref": ref,
                        "owner_key": owner_key, "owner_id": owner_id, "anchored_by": anchored_by,
                        "attribute": attr, "n_owners": n_owners, "status": verdict,
                        "block_id": blocks_ids[0] if blocks_ids else None,
                        # contested attribution is the loud, escalate-class signal (M10c);
                        # ungrounded/unverified are quiet advisories.
                        "strictness": "loud" if verdict == "contested" else "quiet",
                    })

        loud = [e for e in errors if e["strictness"] == "loud"]
        total_checked = sum(v["checked"] for v in metrics.values())
        notes = (f"{total_checked} attribution(s) checked across {len(metrics)} target(s); "
                 f"{len(errors)} non-grounded ({len(loud)} contested/loud). Advisory — "
                 f"M10c routes contested into repair→VMAW→SME." if total_checked
                 else "no attributions to check")
        return {
            "verifier_name": "attribution",
            "passed": True,                            # advisory-first: never hard-fails routing yet
            "field_errors": errors[:100],
            "metrics": metrics,
            "notes": notes,
        }


def _object_props(schema_loader: Any, section: str, array_keys: list[str]) -> set[str]:
    """Descend the schema to the property set of the object at the end of array_keys."""
    node = schema_loader.get_section(section).raw_json_schema
    for k in array_keys:
        node = (node.get("properties") or {}).get(k) or {}
        node = node.get("items") or {}           # array → its item schema
    return set((node.get("properties") or {}).keys())


def validate_against_schema(mapping_path: str, schema_loader: Any) -> list[str]:
    """Completeness check: every target's section exists, and its owner_key +
    attributes are real fields of the object the array_keys path lands on."""
    targets = _load_targets(mapping_path)
    try:
        sections = set(schema_loader.list_sections())
    except Exception:  # noqa: BLE001
        sections = {"report_metadata", "other_molecular_biomarker_umbrella",
                    "tested_biomarker_umbrella", "significant_findings", "clinical_information"}
    issues: list[str] = []
    for t in targets:
        sec = t.get("section")
        if sec not in sections:
            issues.append(f"target {t.get('name')!r} → unknown section {sec!r}")
            continue
        try:
            props = _object_props(schema_loader, sec, t.get("array_keys") or [])
        except Exception as exc:  # noqa: BLE001
            issues.append(f"target {t.get('name')!r} → array_keys not navigable ({exc})")
            continue
        if not props:
            issues.append(f"target {t.get('name')!r} → array_keys {t.get('array_keys')} resolves to no object schema")
            continue
        if t.get("owner_key") not in props:
            issues.append(f"target {t.get('name')!r} → owner_key {t.get('owner_key')!r} not a field of the object")
        for a in (t.get("attributes") or []):
            if a not in props:
                issues.append(f"target {t.get('name')!r} → attribute {a!r} not a field of the object")
    return issues
