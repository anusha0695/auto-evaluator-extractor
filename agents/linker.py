"""
Linker — the unified contextual capability (Phase 2, M5).

One module, three operations + assembly, run AFTER all teams:

  - ASSEMBLE  : wrap every team's section output into the full schema-v3
                envelope (deterministic).
  - LINK      : cross-section relationships. Deterministic gene-key seed
                (variant `gene_studied` ↔ `tested_biomarkers` via HGNC); the
                contextual links (finding ↔ interpretation, biomarker ↔ specimen)
                come from an INJECTED LLM adjudicator.
  - GROUP/dedup-vs-group : deterministic normalized-key SEED only; the ambiguous
                MERGE-vs-KEEP_SEPARATE decision comes from the injected LLM
                adjudicator. (Within-section findings[] grouping is already done
                by each team's Extractor per its prompt.)
  - SUPERSESSION : deterministic detection on `addendum`-tagged blocks. When the
                addendum clearly names (biomarker, method, result) the amended
                value wins and the original is kept with `superseded:true`;
                otherwise the finding is flagged `needs_review` for VMAW/SME.

LLM operations are dependency-injected (`merge_adjudicator`, `link_adjudicator`,
`supersession_resolver`). In deterministic-only mode (no adjudicators — e.g. the
M5 gate) ambiguous cases ESCALATE (flagged `needs_review`) rather than guess.
graph_linear (M8) injects the real Gemini-backed adjudicators. Every escalation
point is catalogued in PHASE_2_VMAW_TRIGGERS.md.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.observability import trace as otel_trace

logger = logging.getLogger(__name__)

# section name → (array_holder_key, item_list_key) for the array umbrellas.
# v3 merge: the former Genomic_Variant_umbrella folded into the biomarker
# umbrella, so gene variants are biomarker entries carrying a `variant_detail`.
_BIOMARKER_SECTION = "other_molecular_biomarker_umbrella"
_TESTED_SECTION = "tested_biomarker_umbrella"


# ---------------------------------------------------------------------------
# Section layout — read once from config/section_layout.yaml. Drives the
# generic linker assembly + count. Adding a new section is one YAML row.
# ---------------------------------------------------------------------------


def _load_section_layout() -> dict[str, dict[str, Any]]:
    import yaml
    from pathlib import Path
    p = Path("config/section_layout.yaml")
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return (raw.get("sections") or {})
    except Exception:  # noqa: BLE001 — degrade to convention-only (empty config)
        logger.exception("linker: failed to read config/section_layout.yaml — falling back to conventions")
        return {}


_SECTION_LAYOUT: dict[str, dict[str, Any]] = _load_section_layout()


def _empty_section(name: str) -> dict[str, Any]:
    """Schema-shaped empty placeholder for an active section a team left empty.
    Reads `sections.<name>.empty` from config/section_layout.yaml. Returns `{}` when
    no layout entry exists (the section validates against a relaxed root schema)."""
    return dict((_SECTION_LAYOUT.get(name) or {}).get("empty") or {})


def _load_dedup_policy() -> list[dict[str, Any]]:
    """Read `config/dedup_policy.yaml:rules`. Returns [] when the file is absent."""
    import yaml
    from pathlib import Path
    p = Path("config/dedup_policy.yaml")
    if not p.exists():
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return list(raw.get("rules") or [])
    except Exception:  # noqa: BLE001 — degrade to no-policy
        logger.exception("linker: failed to read config/dedup_policy.yaml — no dedup applied")
        return []


_DEDUP_POLICY: list[dict[str, Any]] = _load_dedup_policy()


def _section_record_array_field(name: str) -> str | None:
    """The property name of `name`'s record-array (the array `Linker._count_objects`
    sums). Reads `sections.<name>.record_array` from config/section_layout.yaml.
    Returns None when the section has no record array (e.g. `report_metadata`)."""
    return (_SECTION_LAYOUT.get(name) or {}).get("record_array")


def _record_satisfies(record: Any, requires: str) -> bool:
    """True iff the dotted-path `requires` (e.g. `findings[*].variant_detail`) is
    truthy anywhere on `record`. `[*]` means "any element of this list." Used by the
    gene-key seed to filter out non-gene-keyed records (e.g. plain IHC biomarkers in
    v3 — only biomarker findings WITH `variant_detail` should be variant-linked)."""
    def _walk(node: Any, parts: list[str]) -> bool:
        if not parts:
            return bool(node)
        head, *rest = parts
        if head == "*":
            if not isinstance(node, list):
                return False
            return any(_walk(it, rest) for it in node)
        if isinstance(node, dict) and head in node:
            return _walk(node[head], rest)
        return False
    # split dotted path; the "[*]" array-any sentinel becomes a separate token.
    parts: list[str] = []
    for tok in (requires or "").split("."):
        if tok.endswith("[*]"):
            parts.append(tok[:-3])
            parts.append("*")
        else:
            parts.append(tok)
    return _walk(record, parts)


def _scalar_tic_keys() -> frozenset[str]:
    """Schema-scalar field names the model sometimes emits as a 1-element list. Read
    from `config/section_layout.yaml:scalar_keys`. Used by extractor's pre-validation
    coercion. Cached at import time."""
    import yaml
    from pathlib import Path
    p = Path("config/section_layout.yaml")
    if not p.exists():
        return frozenset()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return frozenset(str(k) for k in (raw.get("scalar_keys") or []))
    except Exception:  # noqa: BLE001
        return frozenset()


@dataclass
class Link:
    from_ref: str
    to_ref: str
    type: str                         # e.g. "variant_on_panel"
    rationale: str
    evidence_block_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    method: str = "deterministic"     # "deterministic" | "contextual"


@dataclass
class LinkResult:
    envelope: dict[str, Any]
    links: list[Link] = field(default_factory=list)
    needs_review_refs: list[dict[str, str]] = field(default_factory=list)  # {ref, reason}
    notes: str = ""
    # Trace channels — lifted into the unified agent_trace by linker_node.
    dedup_drops: list[dict[str, Any]] = field(default_factory=list)         # {section, ref, gene, rule}
    supersession_events: list[dict[str, Any]] = field(default_factory=list)  # {ref, addendum_block_id, resolved, detail}
    dropped_contextual_links: list[dict[str, Any]] = field(default_factory=list)  # {from_ref, to_ref, type, reason}


class Linker:
    def __init__(
        self,
        *,
        hgnc_normalize: Callable[[str], dict] | None = None,
        merge_adjudicator: Callable[..., dict] | None = None,
        link_adjudicator: Callable[..., list] | None = None,
        supersession_resolver: Callable[..., dict] | None = None,
        link_registry: Any | None = None,
        min_link_confidence: float = 0.5,
        link_seed_enabled: bool = True,
    ) -> None:
        # default HGNC normalizer (the real tool) unless one is injected (tests).
        if hgnc_normalize is None:
            from preprocess.hgnc_resolver import hgnc_normalize as _h
            hgnc_normalize = _h
        self._hgnc = hgnc_normalize
        self._merge_adjudicator = merge_adjudicator
        self._link_adjudicator = link_adjudicator
        self._supersession_resolver = supersession_resolver
        # P3-M5 contextual-linking config.
        self._registry = link_registry          # LinkRegistry | None (None → legacy validation)
        self._min_link_conf = float(min_link_confidence)
        # Deterministic gene-key seed: a CONFIG TOGGLE (default on, measured).
        # When True the gene-key links are committed (legacy behavior). When
        # False they are passed to the adjudicator as non-binding HINTS only and
        # the agent makes the final call (pure-contextual A/B arm).
        self._link_seed_enabled = bool(link_seed_enabled)

    # -----------------------------------------------------------------------
    # Public
    # -----------------------------------------------------------------------

    @otel_trace("agents.linker.link")
    def link(
        self,
        *,
        sections: dict[str, dict[str, Any]],
        blocks: list[dict[str, Any]] | None = None,
        block_profiles: list[dict[str, Any]] | None = None,
        relink_hints: list[dict[str, Any]] | None = None,
    ) -> LinkResult:
        """Assemble + link the section outputs. `sections` maps schema section
        name → that team's emitted payload. `blocks`/`block_profiles` power
        supersession (addendum detection)."""
        envelope = self._assemble_envelope(sections)
        result = LinkResult(envelope=envelope)

        # LINK (deterministic gene-key seed). P3-M5: the seed is a CONFIG TOGGLE.
        #  - seed ON  → commit the gene-key links (legacy behavior) AND pass them
        #               to the adjudicator as hints.
        #  - seed OFF → DON'T commit; pass them as non-binding hints only and let
        #               the contextual agent decide (pure-contextual A/B arm).
        seed_links = self._gene_key_links(envelope)
        if self._link_seed_enabled:
            result.links.extend(seed_links)
        seed_hints = [
            {"from_ref": l.from_ref, "to_ref": l.to_ref, "type": l.type, "rationale": l.rationale}
            for l in seed_links
        ]

        # LINK (contextual, injected) — the PRIMARY link producer (P3-M5). Fed the
        # typed-link catalog + seed hints; emits typed links. The LLM may propose
        # links with refs it invented or types it shouldn't, so VALIDATE every one
        # deterministically (type in registry + endpoints match + refs resolve +
        # evidence cited + confidence floor) and DROP failures — a bad link is the
        # adjudicator's error, never ground truth. The verifier re-checks too.
        if self._link_adjudicator is not None:
            try:
                existing = {(l.from_ref, l.to_ref, l.type) for l in result.links}
                proposed = self._call_link_adjudicator(envelope, blocks, seed_hints, relink_hints) or []
                kept = dropped = 0
                for lk in proposed:
                    frm, to, typ = lk.get("from_ref"), lk.get("to_ref"), lk.get("type")
                    ok, why = self._validate_contextual_link(lk, envelope, blocks)
                    if not ok:
                        dropped += 1
                        logger.debug("Linker: dropped contextual link (%s): %s", why, lk)
                        result.dropped_contextual_links.append({
                            "from_ref": str(frm or ""), "to_ref": str(to or ""),
                            "type": str(typ or ""), "reason": str(why or "validation failed"),
                            "stage": "validate"})
                        continue
                    if (frm, to, typ) in existing:
                        dropped += 1
                        result.dropped_contextual_links.append({
                            "from_ref": str(frm or ""), "to_ref": str(to or ""),
                            "type": str(typ or ""), "reason": "duplicate of seeded/earlier link",
                            "stage": "dedupe"})
                        continue
                    existing.add((frm, to, typ))
                    result.links.append(Link(method="contextual", **lk))
                    kept += 1
                if proposed:
                    logger.info("Linker: contextual links kept=%d dropped(invalid/dupe)=%d", kept, dropped)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Linker: link_adjudicator failed: %s", exc)

        # SUPERSESSION (deterministic detection on addendum blocks)
        self._apply_supersession(envelope, blocks or [], block_profiles or [], result)

        # DEDUP — the canonical cross-section reconciliation (config/dedup_policy.yaml).
        # Extraction is recall-first: NER and team prompts may legitimately route the
        # same entity to multiple sections. We resolve the overlap HERE, post-assembly,
        # by HGNC-canonical match → drop the lower-priority side. v3 safe (rule's
        # `when_present_in` section is absent in v3 → rule no-ops).
        self._apply_dedup_policy(envelope, result)

        # count_of_extracted_objects
        envelope["count_of_extracted_objects"] = self._count_objects(envelope)
        return result

    # -----------------------------------------------------------------------
    # Contextual-link plumbing (P3-M5)
    # -----------------------------------------------------------------------

    def _call_link_adjudicator(
        self,
        envelope: dict[str, Any],
        blocks: list[dict[str, Any]] | None,
        seed_hints: list[dict[str, Any]],
        relink_hints: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Call the injected adjudicator, feeding the typed-link catalog + seed hints
        + (gap #3) `avoid_hints` = refuted pairings the binding verifier rejected, so a
        re-link RE-EVALUATES them instead of blindly re-emitting. Three-tier fallback
        keeps older/stub adjudicators working: avoid_hints → M5 kwargs → (envelope, blocks)."""
        catalog = self._registry.catalog_text() if self._registry is not None else None
        try:
            return self._link_adjudicator(
                envelope=envelope, blocks=blocks, link_catalog=catalog,
                seed_hints=seed_hints, avoid_hints=relink_hints or [],
            )
        except TypeError:
            pass
        try:
            return self._link_adjudicator(
                envelope=envelope, blocks=blocks, link_catalog=catalog, seed_hints=seed_hints,
            )
        except TypeError:
            return self._link_adjudicator(envelope=envelope, blocks=blocks)

    def _validate_contextual_link(
        self,
        link: dict[str, Any],
        envelope: dict[str, Any],
        blocks: list[dict[str, Any]] | None,
    ) -> tuple[bool, str]:
        """Deterministic safety net for a proposed contextual link. Uses the
        typed registry when present (type + endpoints + evidence + confidence);
        otherwise falls back to the legacy resolve-both-refs check."""
        if self._registry is not None:
            return self._registry.validate_link(
                link=link, envelope=envelope, blocks=blocks,
                min_confidence=self._min_link_conf,
            )
        # Legacy (no registry): just require both refs to resolve.
        if self._ref_resolves(envelope, link.get("from_ref")) and \
           self._ref_resolves(envelope, link.get("to_ref")):
            return True, "ok (legacy)"
        return False, "dangling ref (legacy)"

    # -----------------------------------------------------------------------
    # ASSEMBLE
    # -----------------------------------------------------------------------

    _REF_RE = re.compile(r"^(?P<section>[A-Za-z_]+)\.(?P<arr>[A-Za-z_]+)\[(?P<idx>\d+)\]$")

    @classmethod
    def _ref_resolves(cls, envelope: dict[str, Any], ref: str | None) -> bool:
        """True iff ref like 'Section.array[idx]' points at a real envelope item."""
        if not ref:
            return False
        m = cls._REF_RE.match(ref)
        if not m:
            return False
        section = envelope.get(m.group("section"))
        if not isinstance(section, dict):
            return False
        arr = section.get(m.group("arr"))
        if not isinstance(arr, list):
            return False
        return 0 <= int(m.group("idx")) < len(arr)

    @staticmethod
    def _assemble_envelope(sections: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Wrap section payloads into the full envelope. **Fully generic**: emits exactly
        the sections the active teams produced (in registry order), so adding a new team
        is a pure config change — `teams_*.yaml` lists it, the planner activates it, the
        team's output flows through here untouched. A None payload is replaced with a
        schema-shaped empty placeholder (`_empty_section`). v3 + v4 share this path."""
        env: dict[str, Any] = {"count_of_extracted_objects": 0}
        for name, payload in sections.items():
            env[name] = payload if payload is not None else _empty_section(name)
        return env

    @staticmethod
    def _count_objects(env: dict[str, Any]) -> int:
        """Sum the items across every section's record array (the one array property whose
        items are records — looked up via `_section_record_array_field`). Generic over any
        section that follows the umbrella convention; a new section needs no code edit if
        it's registered in `config/section_layout.yaml`, else falls back to the convention."""
        n = 0
        for sec_name, payload in (env or {}).items():
            if sec_name == "count_of_extracted_objects" or not isinstance(payload, dict):
                continue
            arr_field = _section_record_array_field(sec_name)
            if arr_field is None:
                continue
            arr = payload.get(arr_field)
            if isinstance(arr, list):
                n += len(arr)
        return n

    # -----------------------------------------------------------------------
    # LINK — deterministic gene-key
    # -----------------------------------------------------------------------

    def _canon(self, symbol: str | None) -> str | None:
        """HGNC-canonical gene symbol from a record value. Tries the full string first
        (handles aliases like HER2/neu → ERBB2 via the resolver). If that fails, falls
        back to the first whitespace token — so a biomarker_name like
        `"JAK2 V617F Mutation"` (model added the variant suffix) still resolves to JAK2.
        This is purely a recall/dedup convenience: HGNC equality remains the source of
        truth, but the entry point is forgiving."""
        if not symbol or not isinstance(symbol, str):
            return None
        r = self._hgnc(symbol)
        canon = r.get("canonical")
        if canon:
            return canon
        # Fall back to the first token (case-insensitive). Strips trailing punctuation.
        head = symbol.strip().split()[0] if symbol.strip() else ""
        head = re.sub(r"[^\w-].*$", "", head)  # drop anything after the first non-word char
        if head and head.lower() != symbol.strip().lower():
            r2 = self._hgnc(head)
            return r2.get("canonical")
        return None

    def _gene_key_links(self, env: dict[str, Any]) -> list[Link]:
        """Deterministic gene-key seed — link records that share the same HGNC-
        normalized gene across the section pairings declared in the link registry.

        Fully **config-driven** — no version branches, no hardcoded section/field
        names:
          - active link types come from `self._registry.active_types()`,
          - each section's record array + gene-key field come from
            `config/section_layout.yaml` (`record_array`, `gene_key_field`,
            optional `gene_key_requires` filter).

        Adding a new gene-key-linked section pair is: (a) declare the type in the
        link registry, (b) declare `gene_key_field` on each section in
        section_layout.yaml. No Python edit. v3 (biomarker↔tested for variant
        findings) and v4 (Genomic_Variant↔tested) both fall out of the same loop."""
        links: list[Link] = []
        # Seed uses the registry for its type vocabulary. When the linker was
        # constructed without an explicit registry (legacy validation mode for unit
        # tests), lazy-load `config/link_registry.yaml` ONLY for the seed — we do
        # NOT mutate `self._registry`, so legacy validation behaviour is preserved.
        seed_registry = self._registry
        if seed_registry is None:
            try:
                from agents.link_registry import LinkRegistry
                seed_registry = LinkRegistry.from_path("config/link_registry.yaml", max_tier=2)
            except Exception:  # noqa: BLE001 — no registry available → no seed
                return links
        for ltype in seed_registry.active_types():
            frm_recs, frm_arr = self._gene_keyed_records(env, ltype.from_section)
            to_recs, to_arr = self._gene_keyed_records(env, ltype.to_section)
            if not frm_recs or not to_recs:
                continue
            # Self-typed pairs (from_section == to_section, e.g. variant_superseded_by,
            # superseded_by) are about an ORIGINAL → its addendum amendment. Linking
            # record `i` to itself by HGNC equality alone is nonsense — a self-loop
            # carries no semantic info. Skip i==j when the type is intra-section.
            # Detection of a real supersession lives in Linker._apply_supersession,
            # which inspects addendum block text and only fires when an addendum
            # actually amends a finding.
            self_loop = (ltype.from_section == ltype.to_section)
            for i, gi in frm_recs:
                for j, gj in to_recs:
                    if gi != gj:
                        continue
                    if self_loop and i == j:
                        continue
                    links.append(Link(
                        from_ref=f"{ltype.from_section}.{frm_arr}[{i}]",
                        to_ref=f"{ltype.to_section}.{to_arr}[{j}]",
                        type=ltype.type,
                        rationale=f"Same HGNC-normalized gene '{gi}'.",
                        confidence=1.0, method="deterministic",
                    ))
        return links

    def _gene_keyed_records(
        self, env: dict[str, Any], section: str,
    ) -> tuple[list[tuple[int, str]], str]:
        """Return [(index, canonical_gene_key)] for every record in `section` that
        carries a non-empty gene + (optionally) passes section_layout.gene_key_requires.
        Returns ([], "") when the section isn't gene-keyed. Reads:
          section_layout.<section>.record_array      → which array
          section_layout.<section>.gene_key_field    → which field (or "*" = record IS the key)
          section_layout.<section>.gene_key_requires → optional dotted path that must be truthy."""
        layout = _SECTION_LAYOUT.get(section) or {}
        arr_field = layout.get("record_array")
        gkf = layout.get("gene_key_field")
        if not arr_field or not gkf:
            return [], ""
        records = (env.get(section) or {}).get(arr_field) or []
        requires = str(layout.get("gene_key_requires") or "")
        out: list[tuple[int, str]] = []
        for i, r in enumerate(records):
            if requires and not _record_satisfies(r, requires):
                continue
            raw = r if (gkf == "*") else (r or {}).get(gkf) if isinstance(r, dict) else None
            canon = self._canon(raw)
            if canon:
                out.append((i, canon))
        return out, arr_field

    # -----------------------------------------------------------------------
    # DEDUP — canonical cross-section reconciliation (config-driven)
    # -----------------------------------------------------------------------

    def _apply_dedup_policy(self, env: dict[str, Any], result: LinkResult) -> None:
        """Drop cross-section duplicates per `config/dedup_policy.yaml`. For each rule
        (when_present_in OWNS the entity; drop_from is the lower-priority section), we
        HGNC-canonicalize the gene_key on both sides and remove records from drop_from
        that match an owner. Optional `also_requires_filter_on_drop_from` narrows what
        we drop (so v3 plain IHC biomarkers aren't touched by the variant rule).

        Updates the section's count field if present. Records the dropped count on
        `result.notes` for observability."""
        if not _DEDUP_POLICY:
            return
        dropped_total = 0
        for rule in _DEDUP_POLICY:
            owner_sec = rule.get("when_present_in")
            drop_sec = rule.get("drop_from")
            match = (rule.get("match_on") or "").lower()
            if not (owner_sec and drop_sec) or match != "gene_key":
                continue
            owner_keys = {g for _, g in self._gene_keyed_records(env, owner_sec)[0]}
            if not owner_keys:
                continue  # owning side empty → nothing to dedup against (v3 no-op)
            drop_records, drop_arr = self._gene_keyed_records(env, drop_sec)
            if not drop_arr:
                continue
            require_path = str(rule.get("also_requires_filter_on_drop_from") or "")
            arr = (env.get(drop_sec) or {}).get(drop_arr) or []
            # indices to drop
            drop_idxs = set()
            for idx, gkey in drop_records:
                if gkey not in owner_keys:
                    continue
                rec = arr[idx] if 0 <= idx < len(arr) else None
                if require_path and not _record_satisfies(rec, require_path):
                    continue
                drop_idxs.add(idx)
                # Trace channel: the field-level provenance for why this record went away.
                result.dedup_drops.append({
                    "section": drop_sec,
                    "ref": f"{drop_sec}.{drop_arr}[{idx}]",
                    "gene": gkey,
                    "owning_section": owner_sec,
                    "rule": f"{owner_sec} owns; drop_from {drop_sec}; match=gene_key",
                })
            if not drop_idxs:
                continue
            kept = [r for i, r in enumerate(arr) if i not in drop_idxs]
            (env[drop_sec] or {})[drop_arr] = kept
            # update count_of_* field if present
            count_field = next((k for k in (env[drop_sec] or {}) if k.startswith("count_of_")), None)
            if count_field is not None:
                env[drop_sec][count_field] = len(kept)
            n = len(drop_idxs)
            dropped_total += n
            logger.info(
                "Linker.dedup: rule (%s owns → drop_from %s) removed %d record(s) [genes=%s]",
                owner_sec, drop_sec, n, sorted(g for _, g in drop_records if _ in drop_idxs),
            )
        if dropped_total:
            result.notes = (result.notes + f" | dedup -{dropped_total}").strip(" |")

    # -----------------------------------------------------------------------
    # SUPERSESSION — deterministic detection on addendum blocks
    # -----------------------------------------------------------------------

    def _apply_supersession(
        self,
        env: dict[str, Any],
        blocks: list[dict[str, Any]],
        block_profiles: list[dict[str, Any]],
        result: LinkResult,
    ) -> None:
        """For each `addendum`-tagged block, find biomarker findings whose
        (name, method) the addendum text mentions and reconcile them.

        Deterministic path: when a `supersession_resolver` is injected and can
        parse the amended (name, method, result), the amended value wins
        (original kept `superseded:true`). Otherwise we DETECT + FLAG the finding
        `needs_review` citing the addendum block — never silently rewrite."""
        addendum_ids = {
            bp.get("block_id") for bp in block_profiles
            if bp.get("text_role") == "addendum"
        }
        if not addendum_ids:
            return
        text_by_id = {b.get("block_id"): (b.get("text") or "") for b in blocks}
        addendum_texts = {bid: text_by_id.get(bid, "") for bid in addendum_ids}

        biomarkers = (env.get("other_molecular_biomarker_umbrella") or {}).get("other_molecular_biomarkers") or []
        for bi, bm in enumerate(biomarkers):
            name = (bm.get("biomarker_name") or "")
            if not name:
                continue
            for bid, atext in addendum_texts.items():
                if name.lower() in atext.lower():
                    ref = f"other_molecular_biomarker_umbrella.other_molecular_biomarkers[{bi}]"
                    resolved = None
                    if self._supersession_resolver is not None:
                        try:
                            resolved = self._supersession_resolver(
                                biomarker=bm, addendum_text=atext, addendum_block_id=bid)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Linker: supersession_resolver failed: %s", exc)
                    if resolved and resolved.get("applied"):
                        # Resolver applied the override in-place (mutated bm) and
                        # marked the superseded occurrence; nothing else to do.
                        result.supersession_events.append({
                            "ref": ref, "addendum_block_id": str(bid or ""),
                            "biomarker_name": name, "resolved": True,
                            "detail": f"addendum block {bid} amended this finding (resolver applied)",
                        })
                        continue
                    # Detection-only → flag for VMAW/SME.
                    bm["needs_review"] = True
                    bm["review_reason"] = (
                        f"Addendum block {bid} mentions '{name}' — possible amended "
                        f"result; requires verification (supersession)."
                    )
                    result.needs_review_refs.append(
                        {"ref": ref, "reason": f"addendum {bid} may supersede a finding"})
                    result.supersession_events.append({
                        "ref": ref, "addendum_block_id": str(bid or ""),
                        "biomarker_name": name, "resolved": False,
                        "detail": f"addendum block {bid} mentions '{name}' — needs_review (no resolver)",
                    })
                    break
