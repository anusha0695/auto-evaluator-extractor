"""
Triage agent + defect builder (P3-M6) — the brain of the scoped ping-back loop.

`build_defects(state)` turns the verifier suite's output (schema errors, the
recall-floor misses, binding refuted/uncertain, missing provenance, needs_review
records) into a flat list of typed `Defect`s, each with a stable signature
`(team, target_ref, defect_type)` used by the recur-guard.

`TriageAgent.decide(state)` applies the LOCKED trigger taxonomy — *"is there a
specific instruction that would fix this?"* — to pick a catalog action per defect,
enforces the layered budget (per-team cap, global cap = activated-teams × factor,
recur-guard), and partitions defects into REPAIR requests (addressable + budget
left) vs ESCALATIONS (ambiguous / exhausted / recurred → SME). The selection is a
deterministic policy over a finite catalog (testable, bounded action space); an
optional injected `triage_llm` hook can disambiguate genuinely ambiguous defects
(default None → pure deterministic, so the gate runs offline).

The repair-vs-escalate decision lives HERE (Prereq-2): graph_selfcorrecting's decision_router
(`decide_v3`) is the FINAL accept/escalate call after the loop settles, not a
second repair selector.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from decision.decision_router import DecisionRouter, _TEAM_SECTION

logger = logging.getLogger(__name__)

# section → owning team (inverse of decision_router._TEAM_SECTION).
_SECTION_TEAM = {section: team for team, section in _TEAM_SECTION.items()}

# Catalog actions (the finite, declared action space).
ACTIONS = ("re_extract_team", "reprofile_block", "renormalize_field",
           "re_link", "drop_and_flag", "escalate")


@dataclass
class Defect:
    defect_type: str                 # schema_error|recall_miss_present|block_misroute|missing_provenance|binding_refuted|binding_uncertain|needs_review|link_cannot_form|normalization_invalid|attribution_contested|invalid_hgvs
    section: str | None
    team: str | None
    target_ref: str | None
    detail: str = ""
    candidate_block_ids: list[str] = field(default_factory=list)
    # Content-derived recur-guard key (optional). When set, the recur-guard
    # tracks the underlying DEFECT (e.g. a specific gene-pair link) instead of
    # the envelope INDEX it happens to occupy. Prevents a re-proposed link at a
    # different `links[N]` index from looking "new" and burning the budget. The
    # raw `target_ref` is still used everywhere else (routing, ledger, UI).
    content_signature: str | None = None
    # For link defects (`link_cannot_form`): the link's registry type at the time
    # of detection (e.g. "variant_superseded_by", "tested_to_result"). Propagated
    # into the escalation queue so VMAW's autonomy gate can protect clinically
    # meaningful link types (supersession) from being silently dropped under the
    # `_DROPPABLE_ON_UNRESOLVED` rule.
    link_type: str | None = None
    # For binding/link defects (S2): the underlying verifier check that produced
    # this defect — one of V1|V2|V3|V4. The umbrella defect_type "binding_refuted"
    # covers V1/V2/V3, but the right repair differs by check:
    #   V1/V2 → re_extract_team (agent picked weak evidence; a fresh extraction
    #           with a hint may fix it)
    #   V3    → escalate-only / VMAW (re-extract can't fix a canonicalization-vs-
    #           source mismatch; the canonical form IS the right answer — let
    #           VMAW's CITE capability ground it via the source surface instead)
    check: str | None = None

    @property
    def signature(self) -> str:
        if self.content_signature:
            return f"{self.team}|{self.content_signature}|{self.defect_type}"
        return f"{self.team}|{self.target_ref}|{self.defect_type}"


@dataclass
class TriageDecision:
    route: str                       # "repair" | "done"
    repair_requests: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# Defects that are ADDRESSABLE with a concrete fix instruction → which action.
_ADDRESSABLE = {
    "schema_error": "re_extract_team",
    "recall_miss_present": "re_extract_team",
    "block_misroute": "reprofile_block",      # block tagged with the role but NOT routed to the section → re-profile + re-extract
    "missing_provenance": "re_extract_team",
    "binding_refuted": "re_extract_team",     # ping-back ONCE → recur-guard escalates on repeat
    "link_cannot_form": "re_link",            # a V4 (cross-section link) refutation → re-link, don't re-extract
    "normalization_invalid": "renormalize_field",  # gap #2: canonical differs → re-normalize the field
    "attribution_contested": "re_extract_team",  # M10c: re-extract once → recur-guard → VMAW (cite/va) → SME
}
# Actions that need a targetable team (the rest operate on a ref / block directly).
_TEAM_REQUIRED = {"re_extract_team", "reprofile_block"}
# Deterministic repairs that do NOT count against the global budget. The budget is
# meant to bound LLM cost + prevent runaway loops on hard cases; deterministic
# rewrites have neither problem (no LLM call; natural stopping condition — after
# the rewrite the canonical matches → the same defect can't fire again, which the
# recur-guard catches anyway). Counting them against the cap starves cheap safe
# fixes whenever an expensive one fires first (e.g. normalization_invalid items
# in the live SME queue showing "global repair budget (8) exhausted").
_DETERMINISTIC = {"renormalize_field"}
# Defects that need human judgment → always escalate (skip repair, straight to the
# escalation queue → VMAW). Re-extraction-proof kinds belong here; the agent router
# (triage_llm) can additionally route an addressable defect to 'escalate' per-case.
_ESCALATE_ONLY = {"binding_uncertain", "needs_review", "invalid_hgvs"}


# Per-section field that carries the gene identity for content-signature use.
# Keep in sync with config/section_layout.yaml `gene_key_field`. Used ONLY by the
# recur-guard's content-derived signature for link refutations — runtime gene-key
# resolution still goes through the Linker, which loads section_layout.yaml.
_LINK_GENE_FIELDS = {
    "Genomic_Variant_umbrella":           "gene_studied",
    "other_molecular_biomarker_umbrella": "biomarker_name",
    "tested_biomarker_umbrella":          "*",   # record IS the gene string
}


def _resolve_at(envelope: dict[str, Any], ref: str | None) -> Any:
    """Walk a dotted/indexed ref into the envelope. Returns None when any step
    misses. Pure / never raises."""
    if not ref:
        return None
    node: Any = envelope
    for tok in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]", str(ref)):
        name, idx = tok.group(1), tok.group(2)
        try:
            if name is not None:
                node = node.get(name) if isinstance(node, dict) else None
            else:
                i = int(idx)
                node = node[i] if isinstance(node, list) and 0 <= i < len(node) else None
        except (AttributeError, KeyError, IndexError, TypeError):
            return None
        if node is None:
            return None
    return node


def _gene_key_at(envelope: dict[str, Any], record_ref: str | None) -> str | None:
    """Resolve a record at `record_ref` and return its canonical gene key per
    _LINK_GENE_FIELDS. Returns None when the record / field can't be found."""
    if not record_ref:
        return None
    section = _section_of(record_ref)
    gkf = _LINK_GENE_FIELDS.get(section or "")
    if not gkf:
        return None
    rec = _resolve_at(envelope, record_ref)
    if rec is None:
        return None
    if gkf == "*":
        return str(rec).strip().upper() or None       # tested_biomarkers[] is bare string
    if isinstance(rec, dict):
        val = rec.get(gkf)
        return str(val).strip().upper() if val else None
    return None


def _resolve_link_type(state: dict[str, Any], link_ref: str | None) -> str | None:
    """Resolve the registry `type` of a link at `link_ref` (e.g. 'links[3]'). Used
    by VMAW's autonomy gate to protect supersession-class links from the silent-drop
    path. Returns None when the ref doesn't resolve."""
    if not link_ref:
        return None
    links = state.get("links")
    if not isinstance(links, list):
        links = (state.get("extraction") or {}).get("links")
    m = re.match(r"links\[(\d+)\]", str(link_ref))
    if not m or not isinstance(links, list):
        return None
    i = int(m.group(1))
    if not (0 <= i < len(links)):
        return None
    link = links[i] if isinstance(links[i], dict) else None
    if not link:
        return None
    t = link.get("type")
    return str(t).strip() if t else None


def _link_content_signature(state: dict[str, Any], link_ref: str | None) -> str | None:
    """Stable signature for a link refutation based on its CONTENT (canonical
    gene pair + type), not the array index. Order-tolerant (A↔B == B↔A).
    Returns None when the link or its endpoints can't be resolved — caller
    falls back to the index-based signature."""
    if not link_ref:
        return None
    # state["links"] is the Linker's emitted list (also present in
    # verification_v2.json); fall back to envelope["links"] if not threaded.
    links = state.get("links")
    if not isinstance(links, list):
        links = (state.get("extraction") or {}).get("links")
    m = re.match(r"links\[(\d+)\]", str(link_ref))
    if not m or not isinstance(links, list):
        return None
    i = int(m.group(1))
    if not (0 <= i < len(links)):
        return None
    link = links[i] if isinstance(links[i], dict) else None
    if not link:
        return None
    envelope = state.get("extraction") or {}
    a = _gene_key_at(envelope, link.get("from_ref"))
    b = _gene_key_at(envelope, link.get("to_ref"))
    ltype = str(link.get("type") or "").strip()
    if not (a and b and ltype):
        return None
    lo, hi = sorted([a, b])                            # order-tolerant
    return f"link:{ltype}:{lo}<->{hi}"


def build_defects(state: dict[str, Any]) -> list[Defect]:
    """Translate the current verifier/binding/envelope state into typed defects.
    Pure function — no LLM, no mutation."""
    defects: list[Defect] = []
    scorecards = list(state.get("verifier_scorecards") or [])
    by_name = {s.get("verifier_name"): s for s in scorecards}
    envelope = state.get("extraction") or {}

    # (1) Structural schema errors → addressable re-extract of the owning section.
    sv = by_name.get("schema_validator")
    if sv is not None and not sv.get("passed", True):
        for err in (sv.get("field_errors") or [])[:25]:
            loc = err.get("loc") or err.get("field_name") or ""
            section = _section_of(loc)
            defects.append(Defect(
                defect_type="schema_error", section=section,
                team=_SECTION_TEAM.get(section), target_ref=str(loc),
                detail=str(err.get("msg") or err.get("type") or "schema error")[:200]))

    # (2) Recall-floor LOUD misses. confirmed_miss / unconfirmed-loud = "present,
    # we missed it" → ping-back; absent already dropped by the re-read.
    rf = by_name.get("recall_floor")
    if rf is not None:
        # block_id → its routed sections (target_umbrella_hints), to tell a genuine
        # miss (re-extract) from a Block-Profiler MIS-ROUTE (re-profile + re-extract).
        hints_by_block = {bp.get("block_id"): set(bp.get("target_umbrella_hints") or [])
                          for bp in ((state.get("doc_profile") or {}).get("block_profiles") or [])
                          if isinstance(bp, dict)}
        for m in (rf.get("field_errors") or []):
            if m.get("strictness") != "loud":
                continue                                   # quiet = note only
            if m.get("status") == "absent":
                continue                                   # true negative → accept
            fname = m.get("field_name") or ""
            section = _section_of(fname)
            bid = m.get("block_id")
            hints = hints_by_block.get(bid)
            # the role-block exists but was NOT routed to this section → mis-route.
            misrouted = bool(bid and section and hints is not None and section not in hints)
            defects.append(Defect(
                defect_type="block_misroute" if misrouted else "recall_miss_present",
                section=section, team=_SECTION_TEAM.get(section), target_ref=fname,
                detail=(f"role {m.get('role')} routed away from {section} (mis-route)" if misrouted
                        else f"role {m.get('role')} present but field empty ({m.get('status')})"),
                candidate_block_ids=[bid] if bid else []))

    # (2.5) Attribution contests (P3-M10c). Only CONTESTED (loud) attributions
    # escalate-class; ungrounded/unverified stay advisory. Addressable → re-extract
    # the owning team ONCE; the recur-guard escalates a repeat → VMAW (cite/va) → SME.
    attr = by_name.get("attribution")
    if attr is not None:
        for e in (attr.get("field_errors") or []):
            if e.get("status") != "contested":
                continue
            ref = e.get("ref")
            section = _section_of(ref)
            defects.append(Defect(
                defect_type="attribution_contested", section=section,
                team=_SECTION_TEAM.get(section), target_ref=ref,
                detail=(f"{e.get('attribute')} may not describe {e.get('owner_key')}="
                        f"{e.get('owner_id')} (n_owners={e.get('n_owners')})")[:200],
                candidate_block_ids=[e["block_id"]] if e.get("block_id") else []))

    # (2.6) Normalization: a field whose deterministic canonical differs from the
    # extracted value → renormalize_field (write canonical, keep verbatim occurrence).
    nz = by_name.get("normalization")
    if nz is not None:
        for e in (nz.get("field_errors") or []):
            ref = e.get("ref") or e.get("field_name")
            section = _section_of(ref)
            defects.append(Defect(
                defect_type="normalization_invalid", section=section,
                team=_SECTION_TEAM.get(section), target_ref=ref,
                detail=f"{e.get('normalizer_key')}: {e.get('input')} → {e.get('canonical')}"[:200]))

    # (2.7) HGVS structural-validity floor (V4-M6). A printed change that is genuinely
    # MALFORMED with no canonical → needs human review. Escalate-only (in _ESCALATE_ONLY):
    # NEVER renormalize (there is no canonical to write) and re-extraction can't fix an
    # OCR-garbled token deterministically — route straight to SME.
    hv = by_name.get("hgvs_validity")
    if hv is not None:
        for e in (hv.get("field_errors") or []):
            ref = e.get("ref") or e.get("field_name")
            section = _section_of(ref)
            defects.append(Defect(
                defect_type="invalid_hgvs", section=section,
                team=_SECTION_TEAM.get(section), target_ref=ref,
                detail=f"malformed HGVS in {e.get('leaf')}: {e.get('value')!r}"[:200]))

    # (3) Missing provenance: a biomarker finding has a result but no occurrences.
    for i, bm in enumerate((envelope.get("other_molecular_biomarker_umbrella") or {})
                           .get("other_molecular_biomarkers") or []):
        for j, fnd in enumerate(bm.get("findings") or []):
            if isinstance(fnd, dict) and fnd.get("result") and not (fnd.get("occurrences") or []):
                ref = f"other_molecular_biomarker_umbrella.other_molecular_biomarkers[{i}].findings[{j}]"
                defects.append(Defect(
                    defect_type="missing_provenance",
                    section="other_molecular_biomarker_umbrella",
                    team=_SECTION_TEAM.get("other_molecular_biomarker_umbrella"),
                    target_ref=ref,
                    detail=f"finding '{bm.get('biomarker_name')}' has result but no occurrences[]"))

    # (4) Binding verifier. PREFER the threaded item-level verdicts
    # (state["binding_items"] = [{ref, check, verdict, evidence}]) for TARGETED
    # ping-back: a V4 (cross-section link) refutation → re-link; any other refutation
    # → re-extract the owning team; uncertain → SME. Fall back to the count-only
    # summary when items aren't threaded (no refs → can't target → escalate).
    binding = state.get("binding_verifier") or {}
    binding_items = state.get("binding_items") or []
    if binding_items:
        for it in binding_items:
            verdict = it.get("verdict")
            ref = it.get("ref")
            section = it.get("section") or _section_of(ref or "")
            if verdict == "refuted":
                is_link = str(it.get("check") or "").upper() == "V4"
                # For V4 link refutations, derive a content-based signature so the
                # recur-guard tracks the SAME bad gene-pair across cycles even if
                # the linker re-emits it at a different `links[N]` index.
                content_sig = _link_content_signature(state, ref) if is_link else None
                # Resolve the link's registry type so VMAW's autonomy gate can
                # protect supersession-class links from the silent-drop path.
                ltype = _resolve_link_type(state, ref) if is_link else None
                # S2: carry the underlying verifier check (V1|V2|V3|V4) so the
                # router can distinguish flavors of binding_refuted.
                check_id = str(it.get("check") or "").strip().upper() or None
                defects.append(Defect(
                    defect_type="link_cannot_form" if is_link else "binding_refuted",
                    section=section, team=_SECTION_TEAM.get(section), target_ref=ref,
                    detail=str(it.get("evidence") or "bind not supported")[:200],
                    content_signature=content_sig, link_type=ltype, check=check_id))
            elif verdict == "uncertain":
                defects.append(Defect(
                    defect_type="binding_uncertain", section=section, team=None,
                    target_ref=ref, detail=str(it.get("evidence") or "")[:200]))
    else:
        if int(binding.get("refuted", 0) or 0) > 0:
            defects.append(Defect(
                defect_type="binding_refuted", section=None, team=None,
                target_ref="(binding:refuted)",
                detail=f"{binding['refuted']} refuted bind(s); no item refs → cannot target"))
        if int(binding.get("uncertain", 0) or 0) > 0:
            defects.append(Defect(
                defect_type="binding_uncertain", section=None, team=None,
                target_ref="(binding:uncertain)",
                detail=f"{binding['uncertain']} uncertain bind(s) → SME"))

    # (5) needs_review records (OCR/inference token change) → escalate.
    for item in DecisionRouter._needs_review_items(envelope):
        defects.append(Defect(
            defect_type="needs_review", section=item.get("section"), team=None,
            target_ref=item.get("ref"), detail=str(item.get("detail") or "")[:200]))

    return defects


def _section_of(loc: Any) -> str | None:
    """First path segment of a dotted/loc ref → the schema section."""
    if isinstance(loc, (list, tuple)) and loc:
        return str(loc[0])
    s = str(loc or "")
    if not s:
        return None
    head = s.split(".")[0].split("[")[0]
    return head or None


class TriageAgent:
    """Deterministic triage policy over the declared catalog. Layered budget +
    recur-guard make termination structural (independent of any single cap)."""

    def __init__(
        self,
        *,
        per_team_cap: int = 1,
        global_cap_factor: int = 2,
        triage_llm: Callable[..., str] | None = None,
    ) -> None:
        self._per_team_cap = int(per_team_cap)
        self._factor = int(global_cap_factor)
        self._triage_llm = triage_llm     # optional ambiguity tie-break (unused in deterministic core)

    def _global_cap(self, state: dict[str, Any]) -> int:
        n_active = len(state.get("active_team_keys") or []) or len(_TEAM_SECTION)
        return n_active * self._factor

    def decide(self, state: dict[str, Any]) -> TriageDecision:
        defects = build_defects(state)
        seen = set(state.get("defect_signatures_seen") or [])
        # S4: the per-run global call-budget was removed. We still read
        # `repair_budget_used` only for the legacy backward-compat path of
        # cycle-numbering inside repair_log entries; the deciding policy no
        # longer consults it.
        # per-team repairs already executed (from the repair ledger).
        per_team_done: dict[str, int] = {}
        for e in (state.get("repair_log") or []):
            if e.get("action") == "re_extract_team" and e.get("team"):
                per_team_done[e["team"]] = per_team_done.get(e["team"], 0) + 1

        requests: list[dict[str, Any]] = []
        escalations: list[dict[str, Any]] = []
        notes: list[str] = []

        for d in defects:
            esc = lambda why, _d=d: escalations.append({  # noqa: E731
                "section": _d.section, "ref": _d.target_ref, "kind": _d.defect_type,
                "detail": _d.detail, "reason": why,
                # Carry the link's registry type through to the queue so VMAW's autonomy
                # gate can protect supersession-class links. Absent for non-link defects.
                **({"link_type": _d.link_type} if _d.link_type else {})})

            if d.defect_type in _ESCALATE_ONLY:
                esc("needs human judgment (taxonomy)")
                continue
            # S2: V3 binding_refuted (name-not-found / hallucination check) →
            # escalate straight to VMAW. Re-extracting can't fix a canonicalization-
            # vs-source mismatch: the agent already emitted the canonical form
            # (e.g. "MSI" from source "MICROSATELLITE INSTABILITY") which is the
            # right answer. VMAW's CITE capability is the right tool — it grounds
            # via the source surface rather than the canonical name. V1/V2
            # binding_refuted is handled by the deterministic _ADDRESSABLE map
            # below (re_extract_team) — re-extract CAN fix those (agent picked
            # weak evidence; a hinted re-extract may land better).
            if d.defect_type == "binding_refuted" and d.check == "V3":
                esc("V3 hallucination — re-extract can't fix synonym/canonicalization gap; route to VMAW (CITE)")
                continue
            action = _ADDRESSABLE.get(d.defect_type)
            if action is None:
                esc(f"no catalog action for {d.defect_type}")
                continue
            # Agent router (gaps #6/#7): the deterministic action is the DEFAULT. The
            # optional triage_llm may (a) route to 'escalate' when re-extraction/repair
            # can't fix it (→ VMAW/SME, no wasted cycle — this is why no `invoke_vmaw`
            # type is needed), (b) choose a different CATALOG action, and/or (c) assign a
            # team. All choices are constrained to the declared catalog + active teams and
            # still bounded by the per-team/global caps + recur-guard below.
            if self._triage_llm is not None:
                try:
                    choice = self._triage_llm(
                        defect={"defect_type": d.defect_type, "target_ref": d.target_ref,
                                "detail": d.detail},
                        candidate_teams=list(state.get("active_team_keys") or []),
                        proposed_action=action) or {}
                except Exception:  # noqa: BLE001 — degrade to the deterministic policy
                    choice = {}
                if choice.get("action") == "escalate":
                    esc("agent: " + (choice.get("rationale") or "re-extraction can't fix this")[:140])
                    continue
                if choice.get("action") in ACTIONS:
                    action = choice["action"]
                if choice.get("team"):
                    d.team = choice["team"]
                    notes.append(f"triage_llm assigned team '{d.team}' to {d.target_ref}")
            if action in _TEAM_REQUIRED and d.team is None:
                esc("addressable but no targetable team")
                continue
            # recur-guard: same signature already attempted → escalate (don't loop).
            if d.signature in seen:
                esc("defect signature recurred after a repair")
                continue
            # per-team cap.
            if per_team_done.get(d.team, 0) >= self._per_team_cap:
                esc(f"per-team repair cap ({self._per_team_cap}) reached for {d.team}")
                continue
            # S4: the per-run global call-budget was REMOVED. The previous cap
            # (n_active_teams × factor) could starve later defects when earlier
            # defects consumed the pool before the loop ever reached them
            # (e.g. the MSI biomarker escalating with "global repair budget
            # exhausted" while the biomarker team had not even tried). Cost is
            # already bounded structurally:
            #   • per_team_cap = 1 → at most n_teams re-extracts ever (4 for v4)
            #   • recur-guard escalates the same signature on repeat → bounds
            #     re_link / reprofile_block / renormalize from looping
            #   • GRAPH_RECURSION_LIMIT (graph_selfcorrecting) is the outermost
            #     bound on total triage→repair→verifier cycles
            requests.append({
                "action": action, "team": d.team, "section": d.section,
                "target_ref": d.target_ref, "defect_type": d.defect_type,
                "detail": d.detail, "candidate_block_ids": d.candidate_block_ids,
                "signature": d.signature,
            })

        route = "repair" if requests else "done"
        if not defects:
            notes.append("no defects — clean")
        return TriageDecision(route=route, repair_requests=requests,
                              escalations=escalations, notes=notes)


def make_triage_node(*, agent: TriageAgent | None = None):
    """LangGraph node. Writes repair_requests (for repair_node), appends the
    attempted signatures to defect_signatures_seen (recur-guard memory), and
    appends escalations to the SME queue. The conditional edge routes on whether
    repair_requests is non-empty."""
    agent = agent or TriageAgent()

    async def triage_node(state: dict[str, Any]) -> dict[str, Any]:
        decision = agent.decide(state)
        seen = list(state.get("defect_signatures_seen") or [])
        seen += [r["signature"] for r in decision.repair_requests]
        # Dedupe the SME queue by (kind, ref, section, detail). The verifier→triage
        # pass re-runs after each repair cycle and re-detects the same standing
        # defects, so a naive concat double-counts every still-open item across
        # cycles. Keep first occurrence; preserve order.
        merged = list(state.get("escalation_queue") or []) + decision.escalations
        queue: list[dict[str, Any]] = []
        seen_items: set = set()
        for it in merged:
            sig = (it.get("kind"), it.get("ref"), it.get("section"), it.get("detail"))
            if sig in seen_items:
                continue
            seen_items.add(sig)
            queue.append(it)
        n_dropped = len(merged) - len(queue)
        logger.info("triage: doc_id=%s route=%s repairs=%d escalations=%d (queue=%d, deduped -%d)",
                    state.get("doc_id"), decision.route,
                    len(decision.repair_requests), len(decision.escalations),
                    len(queue), n_dropped)
        # Trace: a single Triage record summarising the decision, plus one record per
        # defect (with the defect's target ref) so a field's timeline shows when triage
        # decided to repair vs escalate THIS field.
        from core.trace_recorder import extend_trace, record
        recs: list[dict[str, Any]] = []
        recs.append(record(
            phase="triage", agent="Triage",
            plain=("Our system classified the open defects and routed each one — either back for "
                   "an automatic repair, or up to a human reviewer."),
            input_summary=f"{len(decision.repair_requests) + len(decision.escalations)} defect(s) considered",
            output_summary=(f"route={decision.route}; {len(decision.repair_requests)} repair(s), "
                            f"{len(decision.escalations)} escalation(s) (dedup -{n_dropped})"),
            verdict=decision.route,
            reasoning="; ".join(decision.notes or [])[:1000]))
        for rr in decision.repair_requests:
            act = rr.get("action", "?")
            recs.append(record(
                phase="triage", agent=f"Triage · repair · {act}",
                plain=f"Triage queued an automatic '{act}' repair for this field.",
                section=rr.get("section"), refs=[rr.get("target_ref") or ""],
                team=str(rr.get("team") or ""),
                input_summary=f"defect_type={rr.get('defect_type','?')}",
                output_summary=f"queued action={act} on team={rr.get('team') or '-'}",
                verdict="repair_queued", reasoning=str(rr.get("detail") or "")))
        for esc in decision.escalations:
            kind = esc.get("kind", "?")
            recs.append(record(
                phase="triage", agent=f"Triage · escalate · {kind}",
                plain=f"Triage flagged this '{kind}' for a human reviewer.",
                section=esc.get("section"), refs=[esc.get("ref") or ""],
                input_summary=f"kind={kind}",
                output_summary=f"to SME — {esc.get('reason','')}",
                verdict="escalated", reasoning=str(esc.get("detail") or "")))
        return {"repair_requests": decision.repair_requests,
                "defect_signatures_seen": seen, "escalation_queue": queue,
                "agent_trace": extend_trace(state.get("agent_trace"), *recs)}

    return triage_node


def triage_route(state: dict[str, Any]) -> str:
    """Conditional-edge selector: 'repair' iff triage queued repair_requests."""
    return "repair" if (state.get("repair_requests") or []) else "done"
