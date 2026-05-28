"""
VMAW — the Verification & Multi-Agent-resolution Worker (P3-M7).

VMAW is the deep resolver on graph_selfcorrecting's escalate branch: it consumes the
`escalation_queue` the repair loop WOULDN'T auto-fix and tries to settle each
item BEFORE a human sees it, so the SME ends up reviewing only what genuinely
can't be decided. Three capabilities (the locked design):

  EC = Expand Context     — re-read with a wider window / cross-references.
  CITE = Evidence Citation — produce the exact span that proves a claim.
  VA = Value Adjudicator  — pick the canonical value when sources conflict, or
                            confirm/override an inferred token.

Autonomy rule (LOCKED with the user):
  * A resolution is AUTO-APPLIED (and dropped from the SME queue) ONLY when it is
    a grounded CONFIRMATION/FILL that passes a DETERMINISTIC grounding re-check —
    the cited block exists AND its text actually contains the resolved value.
    Self-reported LLM confidence never authorizes a write on its own.
  * A CONTESTED VA pick (conflicting values, or overriding an existing value) is
    NEVER auto-shipped — it stays in the SME queue, pre-filled as a VMAW proposal
    (value + citation + rationale) for the human to ratify. (Triggers doc T17:
    "never silently pick.")
  * Ungrounded / no-LLM-hook → stays in the queue (degrade to SME = today's
    behavior).

Cardinal rule: verbatim `occurrences` are NEVER destroyed. A confirmation clears
`needs_review` and attaches a citation; a fill adds a value + provenance; nothing
deletes the literal source.

Capabilities are dependency-injected (`expand_context_fn`, `cite_fn`,
`adjudicate_value_fn`) — default None → VMAW resolves nothing and everything flows
to SME. The cloud deps path wires the real Gemini-backed hooks (lazy, T=0).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from core.observability import trace as otel_trace

logger = logging.getLogger(__name__)

_REF_RE = re.compile(r"([A-Za-z_]+)|\[(\d+)\]")

# escalation kind → ordered capabilities to try (from the T1–T18 capability map).
ROUTING: dict[str, list[str]] = {
    "needs_review": ["cite", "ec"],            # T6 — confirm/ground the inferred token
    "binding_uncertain": ["ec", "cite"],       # T14 — try to ground the narrative bind
    "binding_refuted": ["va", "cite"],         # T13 — adjudicate (contested → SME)
    "recall_floor_loud": ["cite", "ec"],       # missed field — find + ground it
    "recall_miss_present": ["cite", "ec"],
    "dropped_ungroundable": ["cite", "va"],    # can VMAW ground the dropped record?
    "attribution_contested": ["cite", "va"],   # M10c — CITE grounds attr→owner; VA (contested) → SME
    "invoke_vmaw": ["ec", "va"],               # T1 canonical
    "schema_error": ["va"],                    # structural — usually SME
    # Normalization-class defects: the team emitted a value the validator can't
    # canonicalize (HGVS pattern, gene alias resolution, etc). These are CITE-first
    # candidates — VMAW hunts for the canonical form in the source. Without this
    # routing they fall through to _DEFAULT_CAPS=[va] which forces SME even when
    # VMAW lands a grounded+uncontested proposal (a clear LLM-fixable case).
    "invalid_hgvs": ["cite", "ec"],            # malformed HGVS — find the canonical form
    "normalization_invalid": ["cite", "ec"],   # canonical form differs from extracted
    "missing_provenance": ["cite"],            # team emitted value without citation — find it
    "block_misroute": ["ec"],                  # expand context, see if right section is identifiable
    "link_cannot_form": ["va"],                # link endpoint missing — keep VA (judgment)
}
_DEFAULT_CAPS = ["va"]

# gap #5 (locked): a value/record that arrives here is ungroundable iff it already
# went through re-extract + recurrence (triage escalated it). If VMAW ALSO can't
# ground it (status=unresolved), drop the record from the envelope and keep the
# payload in the SME queue for audit/restore. Only these "no-support" kinds drop;
# everything else (needs_review, binding_uncertain, …) stays flagged in the queue.
_DROPPABLE_ON_UNRESOLVED = {"binding_refuted", "link_cannot_form"}


# -- SME queue banding ------------------------------------------------------
#
# Items that survive into `escalation_queue.json` after VMAW STILL need to be
# classified by SME priority. Four bands:
#
#   judgment      — the only band that BLOCKS the workflow. Real decision —
#                    SME must pick between alternatives, ground the value
#                    themselves, or accept VMAW's contested proposal.
#                    Signal: contested=true  OR  (grounded=false  AND  vmaw has a guess)
#
#   unresolved    — VMAW exhausted every capability (EC, CITE, VA) and still
#                    has no proposal. SME has to investigate from scratch.
#                    Signal: vmaw_note.status == "unresolved"
#
#   review_light  — VMAW landed a clean grounded+uncontested proposal but the
#                    capability policy held it back from auto-applying (e.g. VA
#                    for binding_refuted). Safe to one-click approve in batch.
#                    Signal: grounded=true AND contested=false AND has vmaw_proposal
#
#   drop_audit    — the record was dropped from the envelope because VMAW
#                    couldn't ground it after re-extract. SME audits the
#                    deletion (confirm absent, or restore).
#                    Signal: kind == "dropped_ungroundable"
#
# Pure function, no state — UI can call it directly on a queue item.
def classify_band(item: dict[str, Any]) -> str:
    """Return one of: 'judgment' | 'unresolved' | 'review_light' | 'drop_audit'."""
    kind = item.get("kind")
    if kind == "dropped_ungroundable":
        return "drop_audit"
    note = item.get("vmaw_note") or {}
    if note.get("status") == "unresolved":
        return "unresolved"
    prop = item.get("vmaw_proposal") or {}
    if prop and prop.get("grounded") and not prop.get("contested"):
        return "review_light"
    return "judgment"


def band_escalation_queue(queue: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the per-band view used by the SME UI + the persisted
    `escalation_queue_banded.json` artifact. Each item is enriched with
    `triage_band` and grouped under its band; bands are listed in priority
    order so the UI can render them top-to-bottom."""
    banded: dict[str, list[dict[str, Any]]] = {
        "judgment": [], "unresolved": [], "review_light": [], "drop_audit": []}
    for it in (queue or []):
        band = classify_band(it)
        enriched = dict(it)
        enriched["triage_band"] = band
        banded[band].append(enriched)
    return {
        "summary": {
            "total":        sum(len(v) for v in banded.values()),
            "judgment":     len(banded["judgment"]),
            "unresolved":   len(banded["unresolved"]),
            "review_light": len(banded["review_light"]),
            "drop_audit":   len(banded["drop_audit"]),
            "blocks_workflow": len(banded["judgment"]) + len(banded["unresolved"]),
        },
        "bands": banded,
        "band_order": ["judgment", "unresolved", "review_light", "drop_audit"],
    }


@dataclass
class VMAWResolution:
    item_ref: str | None
    kind: str | None
    capability: str | None                # which capability produced the result
    status: str                           # auto_applied | proposed_for_sme | unresolved
    resolved_value: Any = None
    citation_block_ids: list[str] = field(default_factory=list)
    citation_span: str = ""
    grounded: bool = False
    contested: bool = False
    rationale: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_ref": self.item_ref, "kind": self.kind, "capability": self.capability,
            "status": self.status, "resolved_value": self.resolved_value,
            "citation_block_ids": self.citation_block_ids, "citation_span": self.citation_span,
            "grounded": self.grounded, "contested": self.contested,
            "rationale": self.rationale, "confidence": self.confidence,
        }


class VMAWAgent:
    def __init__(
        self,
        *,
        expand_context_fn: Callable[..., dict | None] | None = None,
        cite_fn: Callable[..., dict | None] | None = None,
        adjudicate_value_fn: Callable[..., dict | None] | None = None,
        min_confidence: float = 0.7,
    ) -> None:
        self._hooks = {"ec": expand_context_fn, "cite": cite_fn, "va": adjudicate_value_fn}
        self._min_conf = float(min_confidence)

    # -- per-item resolution -------------------------------------------------

    def resolve_item(
        self, item: dict[str, Any], envelope: dict[str, Any],
        blocks: list[dict[str, Any]], block_reads: dict[str, Any] | None = None,
    ) -> VMAWResolution:
        kind = item.get("kind")
        ref = item.get("ref")
        caps = ROUTING.get(kind, _DEFAULT_CAPS)

        fired = None
        for cap in caps:
            fn = self._hooks.get(cap)
            if fn is None:
                continue
            try:
                out = fn(item=item, envelope=envelope, blocks=blocks, block_reads=block_reads or {})
            except Exception as exc:  # noqa: BLE001 — degrade
                logger.warning("VMAW %s hook failed: %s", cap, exc)
                out = None
            if out and (out.get("value") is not None or out.get("decision_value") is not None):
                fired = (cap, out)
                break

        if fired is None:
            return VMAWResolution(item_ref=ref, kind=kind, capability=None,
                                  status="unresolved", rationale="no capability produced a value")

        cap, out = fired
        value = out.get("value", out.get("decision_value"))
        block_ids = list(out.get("block_ids") or [])
        span = str(out.get("span") or "")
        contested = bool(out.get("contested"))
        conf = float(out.get("confidence") or 0.0)
        grounded = self._grounding_ok(value, block_ids, blocks)

        res = VMAWResolution(
            item_ref=ref, kind=kind, capability=cap, status="unresolved",
            resolved_value=value, citation_block_ids=block_ids, citation_span=span,
            grounded=grounded, contested=contested, rationale=str(out.get("rationale") or ""),
            confidence=conf)

        # ---- the autonomy decision -----------------------------------------
        # Contested adjudication → never silently pick (T17) → SME ratify.
        if contested or cap == "va":
            res.status = "proposed_for_sme"
            return res
        # Grounded confirmation/fill → auto-apply (the only auto path).
        if grounded and conf >= self._min_conf:
            res.status = "auto_applied"
            return res
        # Has a value but ungrounded or low-confidence → propose to SME, don't ship.
        res.status = "proposed_for_sme"
        return res

    # -- batch over the queue ------------------------------------------------

    def resolve(self, state: dict[str, Any]) -> dict[str, Any]:
        queue = list(state.get("escalation_queue") or [])
        envelope = state.get("extraction") or {}
        blocks = (state.get("doc_profile") or {}).get("blocks") or []
        block_reads = state.get("block_reads") or {}

        new_queue: list[dict[str, Any]] = []
        resolutions: list[dict[str, Any]] = []
        log: list[dict[str, Any]] = list(state.get("vmaw_log") or [])
        auto = proposed = unresolved = dropped = 0

        for item in queue:
            res = self.resolve_item(item, envelope, blocks, block_reads)
            resolutions.append(res.to_dict())
            log.append({"item": item, "resolution": res.to_dict()})
            if res.status == "auto_applied":
                self._apply(envelope, item, res)         # mutate ground truth (grounded only)
                auto += 1
                # item leaves the SME queue entirely
            elif res.status == "proposed_for_sme":
                enriched = dict(item)
                enriched["vmaw_proposal"] = {
                    "value": res.resolved_value, "citation_block_ids": res.citation_block_ids,
                    "citation_span": res.citation_span, "rationale": res.rationale,
                    "grounded": res.grounded, "contested": res.contested}
                new_queue.append(enriched)
                proposed += 1
            elif item.get("kind") in _DROPPABLE_ON_UNRESOLVED:
                # gap #5: VMAW couldn't ground an ungroundable record after re-extract +
                # recurrence → DROP it from the envelope; keep the payload for SME audit.
                removed = self._drop_record(envelope, res.item_ref)
                enriched = dict(item)
                enriched["kind"] = "dropped_ungroundable"
                enriched["dropped_record"] = removed
                enriched["vmaw_note"] = {"status": "dropped",
                                         "reason": "VMAW could not ground after re-extract; record removed"}
                new_queue.append(enriched)
                dropped += 1
            else:
                enriched = dict(item)
                enriched["vmaw_note"] = {"status": "unresolved", "tried": ROUTING.get(item.get("kind"), _DEFAULT_CAPS)}
                new_queue.append(enriched)
                unresolved += 1

        logger.info("VMAW: items=%d auto_applied=%d proposed=%d unresolved=%d dropped=%d",
                    len(queue), auto, proposed, unresolved, dropped)
        return {"extraction": envelope, "escalation_queue": new_queue,
                "vmaw_resolutions": resolutions, "vmaw_log": log}

    def _drop_record(self, envelope: dict[str, Any], ref: str | None) -> Any:
        """Remove the record at `ref` from the envelope (list-pop / dict-del), returning
        the removed payload (preserved in the SME queue — never destroyed outright)."""
        node, key = _resolve_parent(envelope, ref)
        try:
            if isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
                return node.pop(key)
            if isinstance(node, dict) and key in node:
                return node.pop(key)
        except (KeyError, IndexError, TypeError):
            return None
        return None

    # -- grounding gate (the safety mechanism) -------------------------------

    @staticmethod
    def _grounding_ok(value: Any, block_ids: list[str], blocks: list[dict[str, Any]]) -> bool:
        """Deterministic re-check: a cited block exists AND its text actually
        contains the resolved value. This — not confidence — authorizes a write."""
        if value is None or not block_ids:
            return False
        text_by_id = {b.get("block_id"): (b.get("text") or "") for b in blocks}
        known = [bid for bid in block_ids if bid in text_by_id]
        if not known:
            return False
        needle = _norm(str(value))
        if not needle:
            return False
        return any(needle in _norm(text_by_id[bid]) for bid in known)

    # -- envelope application (auto_applied only) ----------------------------

    def _apply(self, envelope: dict[str, Any], item: dict[str, Any], res: VMAWResolution) -> None:
        """Apply a GROUNDED confirmation/fill. Never destroys occurrences."""
        kind = item.get("kind")
        node, key = _resolve_parent(envelope, res.item_ref)
        if kind == "needs_review":
            # confirm the inferred token: clear the flag, attach the citation.
            target = _get(node, key)
            if isinstance(target, dict):
                target["needs_review"] = False
                target["vmaw_confirmed"] = {"citation_block_ids": res.citation_block_ids,
                                            "span": res.citation_span, "rationale": res.rationale}
            return
        if kind == "attribution_contested":
            # CITE grounded the attribute→owner pairing. CONFIRM it: attach a citation
            # to the owner object; NEVER overwrite the value (it's already correct) and
            # never destroy occurrences. ref is '<owner>.<attribute>' → node=owner object,
            # key=attribute name.
            if isinstance(node, dict):
                conf = node.setdefault("vmaw_attribution_confirmed", {})
                if isinstance(conf, dict):
                    conf[str(key)] = {"citation_block_ids": res.citation_block_ids,
                                      "span": res.citation_span, "rationale": res.rationale}
            return
        # fill an EMPTY field/record only (never overwrite an existing value here;
        # value changes are routed to SME as contested).
        cur = _get(node, key)
        if cur in (None, "", [], {}):
            _set(node, key, res.resolved_value)


# -- ref helpers ------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).lower()


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


def make_vmaw_node(*, agent: VMAWAgent | None = None):
    """graph_selfcorrecting node on the triage 'done' branch: deep-resolve the escalation
    queue, then control passes to decision_router_v3 (which sees the reduced
    queue + the possibly-improved envelope)."""
    agent = agent or VMAWAgent()

    @otel_trace("pipeline.vmaw.node")
    async def vmaw_node(state: dict[str, Any]) -> dict[str, Any]:
        if not (state.get("escalation_queue") or []):
            return {}                                    # nothing to resolve
        delta = agent.resolve(state)
        # Trace: one record per VMAW resolution step (EC / CITE / VA) with the item's
        # ref. The VMAW agent already writes per-item entries to `vmaw_log` — we lift
        # those into the unified trace so the field timeline shows them as agent steps.
        try:
            from core.trace_recorder import extend_trace, record
            log = delta.get("vmaw_log") or state.get("vmaw_log") or []
            queue = state.get("escalation_queue") or []
            new_entries = log[-len(queue):] if queue else []
            recs: list[dict[str, Any]] = []
            _VMAW_PLAIN = {
                "EC": "VMAW expanded the context window to look at more text around the disputed value.",
                "CITE": "VMAW hunted for a citation that supports (or refutes) the disputed value.",
                "VA": "VMAW adjudicated between conflicting candidate values for the same field.",
            }
            for e in new_entries:
                cap = e.get("step", "?")
                recs.append(record(
                    phase="vmaw", agent=f"VMAW · {cap}",
                    plain=_VMAW_PLAIN.get(cap, f"VMAW ran '{cap}' on this item."),
                    section=e.get("section"), refs=[e.get("ref") or ""],
                    input_summary=f"kind={e.get('kind','?')}",
                    output_summary=str(e.get("outcome") or e.get("status") or ""),
                    verdict=str(e.get("status") or e.get("outcome") or "resolved"),
                    reasoning=str(e.get("rationale") or e.get("detail") or "")))
            if recs:
                delta = {**delta, "agent_trace": extend_trace(state.get("agent_trace"), *recs)}
        except Exception:  # noqa: BLE001
            import logging as _lg
            _lg.getLogger(__name__).exception("vmaw: trace recording failed (non-fatal)")
        return delta

    return vmaw_node
