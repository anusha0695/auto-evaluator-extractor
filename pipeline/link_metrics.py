"""
Link / attribution contested-rate metrics (P3-M10d) — the pruning feed.

`build_link_metrics(state)` aggregates a per-type tally over one run so that, once
the independent holdout exists, a relationship type that is pure noise can be pruned
deterministically (flip `active: false` in config/link_registry.yaml for a link type,
or drop a target from config/attribution_map.yaml). Pure / offline.

Output (`link_metrics.json` artifact):
  {
    "links":       {<link_type>: {emitted, contested, contested_rate}},
    "attribution": {<target>:    {checked, grounded, contested, ungrounded,
                                  unverified, contested_rate}},
    "escalations_by_kind": {<defect_kind>: count},
  }

A committed link is counted "contested" when a binding verdict of refuted/uncertain
touches either endpoint (token overlap). Attribution numbers come straight from the
AttributionVerifier's own per-target `metrics`. Escalations-by-kind tallies whatever
reached the SME queue (binding_refuted, attribution_contested, needs_review, …).
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN_RE = re.compile(r"[A-Za-z_]+\[\d+\]")


def _toks(ref: Any) -> set[str]:
    return set(_TOKEN_RE.findall(str(ref or "")))


def _link_field(link: Any, key: str) -> Any:
    return link.get(key) if isinstance(link, dict) else getattr(link, key, None)


def _rate(n: int, d: int) -> float:
    return round(n / d, 3) if d else 0.0


def build_link_metrics(state: dict[str, Any]) -> dict[str, Any]:
    links = list(state.get("links") or [])
    binding_items = list(state.get("binding_items") or [])
    scorecards = list(state.get("verifier_scorecards") or [])
    queue = list(state.get("escalation_queue") or [])

    # --- links: emitted + contested (binding refuted/uncertain touching an endpoint)
    contested_tok_sets = [_toks(bi.get("ref")) for bi in binding_items
                          if bi.get("verdict") in ("refuted", "uncertain")]
    emitted: dict[str, int] = {}
    contested: dict[str, int] = {}
    for l in links:
        t = _link_field(l, "type")
        if not t:
            continue
        emitted[t] = emitted.get(t, 0) + 1
        et = _toks(_link_field(l, "from_ref")) | _toks(_link_field(l, "to_ref"))
        if et and any(et & cs for cs in contested_tok_sets):
            contested[t] = contested.get(t, 0) + 1
    links_metrics = {
        t: {"emitted": n, "contested": contested.get(t, 0),
            "contested_rate": _rate(contested.get(t, 0), n)}
        for t, n in sorted(emitted.items())
    }

    # --- attribution: straight from the verifier's per-target metrics
    attr_card = next((s for s in scorecards if s.get("verifier_name") == "attribution"), None)
    attribution: dict[str, Any] = {}
    for tgt, m in ((attr_card or {}).get("metrics") or {}).items():
        checked = int(m.get("checked", 0) or 0)
        attribution[tgt] = {
            "checked": checked, "grounded": int(m.get("grounded", 0) or 0),
            "contested": int(m.get("contested", 0) or 0),
            "ungrounded": int(m.get("ungrounded", 0) or 0),
            "unverified": int(m.get("unverified", 0) or 0),
            "contested_rate": _rate(int(m.get("contested", 0) or 0), checked),
        }

    # --- escalations reaching the SME queue, by defect kind
    esc: dict[str, int] = {}
    for it in queue:
        k = it.get("kind")
        if k:
            esc[k] = esc.get(k, 0) + 1

    return {"links": links_metrics, "attribution": attribution,
            "escalations_by_kind": dict(sorted(esc.items()))}
