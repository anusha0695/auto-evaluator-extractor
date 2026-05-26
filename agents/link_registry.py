"""
Typed link registry (P3-M5) — loader, prompt catalog, and the deterministic
validation that wraps the contextual Linking agent.

Every link the LLM adjudicator proposes is decided CONTEXTUALLY, but its SAFETY
is deterministic: `validate_link` drops any proposed link whose `type` is
unknown or inactive, whose endpoint sections don't match the registered pair,
whose refs don't resolve in the envelope, that cites no real evidence block, or
that falls below the confidence floor. A link that can't pass never becomes
ground truth.

`catalog_text` renders the active types as the vocabulary block injected into the
adjudicator prompt. `validate_against_schema` is the completeness check used by
the M5 gate (no registered endpoint references a non-existent schema section).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = "config/link_registry.yaml"

# A ref is a dotted path with optional [idx] array segments at ANY depth, e.g.
#   significant_findings.specimen_findings[0]                       (shallow record)
#   significant_findings.specimen_findings[0].gross_description     (deep sub-object)
#   significant_findings.specimen_findings[0].specimen[1].tissue_type
# `_ref_section` returns the TOP-LEVEL section (first token) for endpoint validation
# (so sf↔sf intra-finding links validate exactly as before); `_ref_resolves` walks
# the WHOLE path against the envelope (M10b — was shallow `section.array[idx]` only).
_PATH_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


@dataclass(frozen=True)
class LinkType:
    type: str
    tier: int
    active: bool
    from_section: str
    to_section: str
    description: str = ""

    @property
    def endpoints(self) -> frozenset[str]:
        """Order-tolerant endpoint set used for validation."""
        return frozenset({self.from_section, self.to_section})


class LinkRegistry:
    """Loaded link-type catalog. One per pipeline boot."""

    def __init__(self, types: list[LinkType], *, max_tier: int = 2) -> None:
        self._all = {t.type: t for t in types}
        self._max_tier = max_tier

    @classmethod
    def from_path(cls, path: str | None = None, *, max_tier: int = 2) -> LinkRegistry:
        path = path or DEFAULT_REGISTRY_PATH
        raw = yaml.safe_load(open(path, encoding="utf-8")) or {}
        types = [
            LinkType(
                type=str(r["type"]), tier=int(r.get("tier", 1)),
                active=bool(r.get("active", True)),
                from_section=str(r["from_section"]), to_section=str(r["to_section"]),
                description=str(r.get("description", "")).strip(),
            )
            for r in (raw.get("link_types") or [])
        ]
        return cls(types, max_tier=max_tier)

    # -- queries -------------------------------------------------------------

    def active_types(self) -> list[LinkType]:
        """Types that are active AND within the configured max tier."""
        return [t for t in self._all.values() if t.active and t.tier <= self._max_tier]

    def get(self, type_name: str | None) -> LinkType | None:
        return self._all.get(type_name) if type_name else None

    def is_emittable(self, type_name: str | None) -> bool:
        t = self.get(type_name)
        return bool(t and t.active and t.tier <= self._max_tier)

    def catalog_text(self) -> str:
        """Vocabulary block for the adjudicator prompt — one line per active type."""
        lines = [
            f"- {t.type} ({t.from_section} ↔ {t.to_section}): {t.description}"
            for t in sorted(self.active_types(), key=lambda x: (x.tier, x.type))
        ]
        return "\n".join(lines)

    # -- validation ----------------------------------------------------------

    def validate_link(
        self,
        *,
        link: dict[str, Any],
        envelope: dict[str, Any],
        blocks: list[dict[str, Any]] | None = None,
        min_confidence: float = 0.0,
    ) -> tuple[bool, str]:
        """Return (ok, reason). ok=True means the link is safe to commit."""
        typ = link.get("type")
        lt = self.get(typ)
        if lt is None:
            return False, f"unknown link type {typ!r}"
        if not (lt.active and lt.tier <= self._max_tier):
            return False, f"link type {typ!r} inactive / above max_tier {self._max_tier}"

        frm, to = link.get("from_ref"), link.get("to_ref")
        if not _ref_resolves(envelope, frm):
            return False, f"from_ref does not resolve: {frm!r}"
        if not _ref_resolves(envelope, to):
            return False, f"to_ref does not resolve: {to!r}"

        # endpoint sections must match the registered (order-tolerant) pair
        got = frozenset({_ref_section(frm), _ref_section(to)})
        if got != lt.endpoints:
            return False, (
                f"endpoint sections {sorted(got)} != registry "
                f"{sorted(lt.endpoints)} for {typ!r}"
            )

        # must cite real evidence (grounded) when blocks are available
        ev = link.get("evidence_block_ids") or []
        if not ev:
            return False, "no evidence_block_ids cited"
        if blocks is not None:
            known = {b.get("block_id") for b in blocks}
            if not all(e in known for e in ev):
                return False, f"evidence cites unknown block(s): {[e for e in ev if e not in known]}"

        conf = link.get("confidence")
        if isinstance(conf, (int, float)) and conf < min_confidence:
            return False, f"confidence {float(conf):.2f} < floor {min_confidence:.2f}"

        return True, "ok"


# -- module-level ref helpers (shared shape with Linker) --------------------


def _ref_tokens(ref: str | None) -> list[Any]:
    """Tokenize a ref into ordered str keys + int indices, e.g.
    'a.b[0].c' → ['a','b',0,'c']. Returns [] for a malformed ref (stray chars,
    bad separators) so a bad ref never silently 'resolves'."""
    if not ref or not isinstance(ref, str):
        return []
    toks: list[Any] = []
    pos = 0
    for m in _PATH_TOKEN_RE.finditer(ref):
        if ref[pos:m.start()] not in ("", "."):     # only '.' may separate tokens
            return []
        name, idx = m.group(1), m.group(2)
        toks.append(name if name is not None else int(idx))
        pos = m.end()
    return toks if pos == len(ref) else []


def _ref_section(ref: str | None) -> str | None:
    toks = _ref_tokens(ref)
    return toks[0] if toks and isinstance(toks[0], str) else None


def _ref_resolves(envelope: dict[str, Any], ref: str | None) -> bool:
    """Walk the full deep path against the envelope; True iff every segment exists."""
    toks = _ref_tokens(ref)
    if not toks or not isinstance(toks[0], str):
        return False
    node: Any = envelope
    for t in toks:
        if isinstance(t, str):
            if not isinstance(node, dict) or t not in node:
                return False
            node = node[t]
        else:                                        # int array index
            if not isinstance(node, list) or not (0 <= t < len(node)):
                return False
            node = node[t]
    return True


def validate_against_schema(registry_path: str, schema_loader: Any) -> list[str]:
    """Completeness check: every registered endpoint names a real schema section.
    Returns a list of issue strings ([] == clean)."""
    raw = yaml.safe_load(open(registry_path, encoding="utf-8")) or {}
    try:
        sections = set(schema_loader.list_sections())
    except Exception:  # noqa: BLE001 — fall back to the v3 section set
        sections = {
            "report_metadata", "other_molecular_biomarker_umbrella",
            "tested_biomarker_umbrella", "significant_findings", "clinical_information",
        }
    issues: list[str] = []
    for r in (raw.get("link_types") or []):
        for side in ("from_section", "to_section"):
            sec = r.get(side)
            if sec not in sections:
                issues.append(f"link type {r.get('type')!r} → unknown section {sec!r} ({side})")
    return issues
