"""
LLM adjudicators — the contextual Gemini calls injected into the Linker and the
Link-&-Binding Verifier (Phase 2 M5/M6 DI hooks).

These are **separate, focused** Gemini calls (T=0.0, Vertex via the same
`ChatGoogleGenerativeAI` wrapper the agents use) that handle the genuinely
contextual decisions the deterministic layer escalates:

  - relationship_confirm (V2) : does this span state {method}→{result} for {name},
                                not a neighbouring biomarker?  confirmed|refuted|uncertain
  - link_confirm        (V4)  : is this cross-section link supported by evidence?
  - link_adjudicator          : propose contextual cross-section links (finding ↔
                                interpretation; biomarker ↔ specimen) beyond the
                                deterministic gene-key seed.
  - supersession_resolver     : given an addendum that names a finding, apply the
                                amended value (mutates the biomarker in place,
                                marks the superseded occurrence) or decline.
  - merge_adjudicator         : MERGE vs KEEP_SEPARATE for an ambiguous dedup pair.

All are **synchronous** (the Linker/Verifier call them inline) and **lazy** (the
LLM client is constructed on first call, so importing/wiring does not touch the
network — keeps the deterministic gates offline). When Gemini is unavailable or
errors, each returns the conservative ESCALATE result (uncertain / applied:false
/ no links), so behaviour degrades to the deterministic-only path rather than
guessing. Enable/disable via env `LLM_ADJUDICATORS` (default on).

This is the Binder/Verifier "two independent LLMs" design: the verifier
adjudicators (relationship_confirm/link_confirm) are a different prompt + call
from the Linker adjudicators, so a confident-but-wrong bind is caught, not
rubber-stamped.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured-output response models
# ---------------------------------------------------------------------------


class _VerdictOut(BaseModel):
    verdict: Literal["confirmed", "refuted", "uncertain"] = "uncertain"
    evidence: str = ""


class _LinkOut(BaseModel):
    from_ref: str
    to_ref: str
    type: str
    rationale: str = ""
    evidence_block_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.7


class _LinksOut(BaseModel):
    links: list[_LinkOut] = Field(default_factory=list)


class _SupersessionOut(BaseModel):
    applied: bool = False
    method: str | None = None
    amended_result: str | None = None
    superseded_result: str | None = None
    rationale: str = ""


class _MergeOut(BaseModel):
    decision: Literal["MERGE", "KEEP_SEPARATE"] = "KEEP_SEPARATE"
    rationale: str = ""


class _AttributionOut(BaseModel):
    # P3-M10: does the attribute describe THIS owner per the source?
    verdict: Literal["grounded", "contested", "ungrounded"] = "ungrounded"
    evidence: str = ""


class _CiteOut(BaseModel):
    # VMAW CITE / EC: the exact span that grounds a value to its claim.
    value: str | None = None
    block_ids: list[str] = Field(default_factory=list)
    span: str = ""
    confidence: float = 0.0
    rationale: str = ""


class _VAOut(BaseModel):
    # VMAW VA: pick/confirm the canonical value; contested=True if sources conflict.
    decision_value: str | None = None
    block_ids: list[str] = Field(default_factory=list)
    contested: bool = True
    confidence: float = 0.0
    rationale: str = ""


class _RereadOut(BaseModel):
    # Recall-floor re-read: is a concrete value for the field actually IN the block?
    present: bool = True            # default present → conservative (keep the miss)
    evidence: str = ""


class _TriageRouteOut(BaseModel):
    # Triage router: pick a catalog ACTION (incl. 'escalate' → VMAW/SME when
    # re-extraction/repair can't fix it) + a team when the action needs one.
    action: str = ""                # "" → keep the deterministic proposal
    team: str = ""                  # "" → none / not needed
    rationale: str = ""


# the declared catalog the agent may choose from (mirrors triage.ACTIONS).
_TRIAGE_ACTION_NAMES = {"re_extract_team", "reprofile_block", "renormalize_field",
                        "re_link", "drop_and_flag", "escalate"}


# ---------------------------------------------------------------------------
# Sync structured Gemini caller (lazy, Vertex via langchain-google-genai)
# ---------------------------------------------------------------------------


def _blocks_text(blocks: list[dict[str, Any]], *, limit: int = 6000) -> str:
    parts = []
    for b in blocks or []:
        t = (b.get("text") or "").strip()
        if t:
            parts.append(f"[block {b.get('block_id')}] {t}")
    return "\n".join(parts)[:limit]


class _Adjudicator:
    """Holds model config; builds the LLM lazily on each call (stateless)."""

    def __init__(self, *, model_name: str | None = None, temperature: float = 0.0) -> None:
        self._model = model_name or os.environ.get("GEMINI_FLASH_MODEL", "gemini-2.5-flash")
        self._temp = temperature

    def call(self, prompt: str, response_model: type[BaseModel]) -> BaseModel | None:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
            llm = ChatGoogleGenerativeAI(model=self._model, temperature=self._temp)
            structured = llm.with_structured_output(response_model, method="function_calling")
            out = structured.invoke(prompt)
            if isinstance(out, response_model):
                return out
            return response_model.model_validate(out)
        except Exception as exc:  # noqa: BLE001 — degrade to escalate
            logger.warning("adjudicator LLM call failed (%s) → escalate", exc)
            return None


# ---------------------------------------------------------------------------
# The five callables (closures over one _Adjudicator)
# ---------------------------------------------------------------------------


def build_llm_adjudicators(*, model_name: str | None = None, temperature: float = 0.0) -> dict[str, Any]:
    """Return the 5 adjudicator callables matching the Linker/Verifier hooks.
    Construction does NOT touch the network — the LLM is built per call."""
    adj = _Adjudicator(model_name=model_name, temperature=temperature)

    def relationship_confirm(*, name: str, finding: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
        method = finding.get("method"); result = finding.get("result")
        cited = [o.get("block_id") for o in (finding.get("occurrences") or [])]
        cited_blocks = [b for b in (blocks or []) if b.get("block_id") in set(cited)] or blocks
        prompt = (
            "You verify a biomarker binding against the source. Answer whether the "
            "cited text states that the given METHOD produced the given RESULT for "
            "the given BIOMARKER — and not for a different biomarker in the same span.\n\n"
            f"BIOMARKER: {name}\nMETHOD: {method}\nRESULT: {result}\n\n"
            f"CITED SOURCE:\n{_blocks_text(cited_blocks)}\n\n"
            "verdict=confirmed only if the span clearly attributes RESULT (via METHOD) "
            "to THIS biomarker; refuted if it attributes it to a different biomarker or "
            "contradicts it; uncertain if the span is insufficient. Give a short evidence quote."
        )
        out = adj.call(prompt, _VerdictOut)
        return (out.model_dump() if out else {"verdict": "uncertain", "evidence": "LLM unavailable → escalate"})

    def link_confirm(*, link: Any, envelope: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
        lk = link if isinstance(link, dict) else vars(link)
        prompt = (
            "You verify a cross-section link in a pathology extraction. Is the stated "
            "relationship supported by the source / the linked records?\n\n"
            f"LINK: from={lk.get('from_ref')} to={lk.get('to_ref')} type={lk.get('type')} "
            f"rationale={lk.get('rationale')}\n\n"
            f"SOURCE (excerpt):\n{_blocks_text(blocks)}\n\n"
            "verdict=confirmed if supported, refuted if contradicted, uncertain otherwise. "
            "Short evidence quote."
        )
        out = adj.call(prompt, _VerdictOut)
        return (out.model_dump() if out else {"verdict": "uncertain", "evidence": "LLM unavailable → escalate"})

    def link_adjudicator(
        *,
        envelope: dict[str, Any],
        blocks: list[dict[str, Any]] | None = None,
        link_catalog: str | None = None,
        seed_hints: list[dict[str, Any]] | None = None,
        avoid_hints: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        # P3-M5: the PRIMARY link producer. Given the typed-link CATALOG (the only
        # allowed `type` values) + optional deterministic seed HINTS, emit typed,
        # grounded links. The Linker validates every one against the registry and
        # drops the rest, so over-emitting is safe but ungrounded links are wasted.
        biomarkers = (envelope.get("other_molecular_biomarker_umbrella") or {}).get("other_molecular_biomarkers") or []
        specimens = (envelope.get("significant_findings") or {}).get("specimen_findings") or []
        if not biomarkers and not specimens:
            return []
        catalog_block = (
            f"ALLOWED LINK TYPES (emit ONLY these, with the exact section endpoints shown):\n{link_catalog}\n\n"
            if link_catalog else
            "Propose contextual links (finding↔interpretation, biomarker↔specimen).\n\n"
        )
        hints_block = (
            f"DETERMINISTIC SEED HINTS (candidate links — confirm or reject from context, "
            f"do not blindly accept):\n{json.dumps(seed_hints)[:1000]}\n\n"
            if seed_hints else ""
        )
        avoid_block = (
            f"PREVIOUSLY REFUTED (the evidence check REJECTED these on the last pass — "
            f"do NOT re-emit them unless the source clearly supports them; prefer a "
            f"different pairing):\n{json.dumps(avoid_hints)[:1000]}\n\n"
            if avoid_hints else ""
        )
        prompt = (
            "You build the relationship graph for a pathology extraction. "
            + catalog_block + hints_block + avoid_block +
            "Rules: (1) emit ONLY a link `type` from the catalog above; (2) refs look like "
            "'other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]' or "
            "'significant_findings.specimen_findings[1]'; (3) NEVER invent a link — emit one "
            "only if you can cite the block_id(s) that state it, in evidence_block_ids; "
            "(4) set confidence in [0,1].\n\n"
            f"BIOMARKERS: {json.dumps([{'name': b.get('biomarker_name')} for b in biomarkers])[:1000]}\n"
            f"SPECIMENS: {json.dumps([{'specimen': s.get('specimen')} for s in specimens])[:1000]}\n\n"
            f"SOURCE (excerpt):\n{_blocks_text(blocks or [])}"
        )
        out = adj.call(prompt, _LinksOut)
        if not out:
            return []
        return [l.model_dump() for l in out.links]

    def supersession_resolver(*, biomarker: dict[str, Any], addendum_text: str, addendum_block_id: str) -> dict[str, Any]:
        name = biomarker.get("biomarker_name")
        prompt = (
            "An addendum may amend an earlier result for a biomarker. Determine whether "
            "the addendum clearly states a NEW (amended) result, and for which method.\n\n"
            f"BIOMARKER: {name}\nADDENDUM TEXT:\n{addendum_text}\n\n"
            "applied=true ONLY if the addendum clearly amends a result for this biomarker; "
            "then give method + amended_result (+ the superseded_result if stated). "
            "If ambiguous, applied=false (it will be escalated to review)."
        )
        out = adj.call(prompt, _SupersessionOut)
        if not out or not out.applied or not out.amended_result:
            return {"applied": False}
        # Apply in place: mark matching finding's occurrences superseded, set new result.
        for f in (biomarker.get("findings") or []):
            if out.method is None or (f.get("method") or "").lower() == out.method.lower():
                for o in (f.get("occurrences") or []):
                    o["superseded"] = True
                f.setdefault("occurrences", []).append({
                    "block_id": addendum_block_id, "surface": out.amended_result, "superseded": False})
                f["result"] = out.amended_result
                return {"applied": True, "method": out.method, "amended_result": out.amended_result}
        return {"applied": False}

    def merge_adjudicator(*, mention_a: dict[str, Any], mention_b: dict[str, Any], spans: str = "") -> dict[str, Any]:
        prompt = (
            "Decide whether two biomarker mentions refer to the SAME test (MERGE) or "
            "DIFFERENT tests (KEEP_SEPARATE) — consider method synonyms, result surface "
            "differences ('2+' vs 'equivocal'), and specimen/timepoint.\n\n"
            f"MENTION A: {json.dumps(mention_a)[:800]}\nMENTION B: {json.dumps(mention_b)[:800]}\n"
            f"SOURCE SPANS:\n{spans[:2000]}"
        )
        out = adj.call(prompt, _MergeOut)
        return (out.model_dump() if out else {"decision": "KEEP_SEPARATE", "rationale": "LLM unavailable → keep separate"})

    return {
        "relationship_confirm": relationship_confirm,
        "link_confirm": link_confirm,
        "link_adjudicator": link_adjudicator,
        "supersession_resolver": supersession_resolver,
        "merge_adjudicator": merge_adjudicator,
    }


def llm_adjudicators_enabled() -> bool:
    """Env gate: LLM_ADJUDICATORS=0/false/no disables (default enabled)."""
    return os.environ.get("LLM_ADJUDICATORS", "1").lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# P3-M10: attribution hook (owner-keyed) + VMAW deep-resolution hooks (EC/CITE/VA)
# ---------------------------------------------------------------------------


def _ref_value(envelope: dict[str, Any], ref: str | None) -> Any:
    """Walk a dotted/[idx] ref to its current value in the envelope (best-effort)."""
    if not ref:
        return None
    import re as _re
    node: Any = envelope
    for tok in _re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]", str(ref)):
        name, idx = tok.group(1), tok.group(2)
        try:
            node = node[name] if name is not None else node[int(idx)]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def build_attribution_fn(*, model_name: str | None = None, temperature: float = 0.0):
    """Gemini-backed owner-keyed attribution check (P3-M10b/c). Returns one of
    'grounded' | 'contested' | 'ungrounded'. Lazy + degrades to 'ungrounded' on
    failure (advisory, never auto-confirms on an LLM error). Signature accepts the
    candidate block TEXTS (`candidate_blocks`); the verifier falls back to the
    id-only signature for legacy/stub hooks."""
    adj = _Adjudicator(model_name=model_name, temperature=temperature)

    def attribution_fn(*, owner_key, owner_id, anchored_by, attribute, value,
                       candidate_block_ids=None, candidate_blocks=None, n_owners=1) -> str:
        text = _blocks_text(candidate_blocks or [])
        prompt = (
            "You verify ATTRIBUTION in a pathology extraction: does the ATTRIBUTE value "
            "describe THIS record, or could it belong to a neighbouring record of the "
            "same kind?\n\n"
            f"RECORD IDENTITY: {owner_key} = {owner_id} (anchored by {anchored_by}; "
            f"{n_owners} sibling record(s) of this kind in scope)\n"
            f"ATTRIBUTE: {attribute} = {value}\n\n"
            f"CITED SOURCE:\n{text}\n\n"
            "verdict=grounded ONLY if the source clearly states this attribute for THIS "
            "record; contested if the value could equally describe a different sibling "
            "record (ambiguous attribution); ungrounded if the source doesn't support it. "
            "Give a short evidence quote."
        )
        out = adj.call(prompt, _AttributionOut)
        return (out.verdict if out else "ungrounded")

    return attribution_fn


def build_vmaw_hooks(*, model_name: str | None = None, temperature: float = 0.0) -> dict[str, Any]:
    """Gemini-backed VMAW capabilities (EC / CITE / VA), matching VMAWAgent's hook
    signature `fn(*, item, envelope, blocks, block_reads)`. CITE/EC return a grounded
    value+span; VA picks/confirms a value and flags `contested`. All lazy + degrade to
    None (→ VMAW leaves the item for SME). The grounding GATE in VMAWAgent still has
    the final say on auto-apply — these only propose."""
    adj = _Adjudicator(model_name=model_name, temperature=temperature)

    def _claim(item: dict[str, Any], envelope: dict[str, Any]) -> str:
        cur = _ref_value(envelope, item.get("ref"))
        return (f"KIND: {item.get('kind')}\nFIELD REF: {item.get('ref')}\n"
                f"CURRENT VALUE: {cur}\nNOTE: {item.get('detail') or ''}")

    def cite_fn(*, item, envelope, blocks, block_reads=None) -> dict | None:
        prompt = (
            "You are VMAW-CITE. Find the EXACT span in the source that proves the claim "
            "below. Do NOT invent — if no span supports it, return value=null.\n\n"
            f"{_claim(item, envelope)}\n\nSOURCE:\n{_blocks_text(blocks)}\n\n"
            "Return the grounded value, the block_ids it appears in, and the verbatim span."
        )
        out = adj.call(prompt, _CiteOut)
        return out.model_dump() if (out and out.value) else None

    def expand_context_fn(*, item, envelope, blocks, block_reads=None) -> dict | None:
        prompt = (
            "You are VMAW-EC (expand context). Re-read the WIDER source (cross-references, "
            "neighbouring sections) to resolve the claim below. Return a grounded value + "
            "span only if the broader context settles it; else value=null.\n\n"
            f"{_claim(item, envelope)}\n\nSOURCE:\n{_blocks_text(blocks)}"
        )
        out = adj.call(prompt, _CiteOut)
        return out.model_dump() if (out and out.value) else None

    def adjudicate_value_fn(*, item, envelope, blocks, block_reads=None) -> dict | None:
        prompt = (
            "You are VMAW-VA (value adjudicator). The claim below is contested or "
            "ambiguous. Pick the single canonical value the source best supports, and set "
            "contested=true if more than one value is plausible (do NOT silently overwrite "
            "an existing value — flag it).\n\n"
            f"{_claim(item, envelope)}\n\nSOURCE:\n{_blocks_text(blocks)}"
        )
        out = adj.call(prompt, _VAOut)
        return out.model_dump() if (out and out.decision_value is not None) else None

    return {"expand_context_fn": expand_context_fn, "cite_fn": cite_fn,
            "adjudicate_value_fn": adjudicate_value_fn}


def build_recall_reread_fn(*, model_name: str | None = None, temperature: float = 0.0):
    """Gemini-backed recall-floor re-read (P3-M3/M6 capability, finally wired). Given a
    block + a field, returns True if a concrete value for that field IS present in the
    block (→ a CONFIRMED miss the loop should fix) or False if genuinely absent (→ a true
    negative the floor drops). DEGRADES to True (keep the miss) when the block isn't
    visible or the LLM errors — the conservative direction for a recall floor: never
    silently drop a real miss. Signature matches RecallFloorVerifier's `reread_fn`
    (block_id/section/field_path) plus an injected `blocks` (the verifier node supplies
    the run's blocks)."""
    adj = _Adjudicator(model_name=model_name, temperature=temperature)

    def reread(*, block_id, section, field_path, blocks=None) -> bool:
        text = next((b.get("text") or "" for b in (blocks or []) if b.get("block_id") == block_id), "")
        if not text:
            return True                                   # can't see the block → keep the miss
        fld = ".".join(field_path) if isinstance(field_path, (list, tuple)) else str(field_path)
        prompt = (
            "You re-read ONE report block to settle whether our extractor missed a value. "
            f"Does this block state a concrete value for the field '{section}.{fld}'?\n\n"
            f"BLOCK:\n{text[:4000]}\n\n"
            "present=true ONLY if a real value for that field appears in this block; "
            "present=false if the block does not actually state it."
        )
        out = adj.call(prompt, _RereadOut)
        return bool(out.present) if out else True         # LLM error → keep the miss

    return reread


def build_triage_llm(*, model_name: str | None = None, temperature: float = 0.0):
    """Gemini-backed triage ROUTER (gaps #6/#7). The deterministic policy proposes an
    action; this agent may (a) ROUTE TO 'escalate' when re-extraction/repair can't fix
    the defect (it then flows to VMAW/SME — no wasted repair cycle), or (b) pick a team
    when the deterministic map couldn't, or (c) keep the proposal. Choice is CONSTRAINED
    to the declared catalog + validated against the active teams (caller re-checks); the
    budget caps + recur-guard still bound everything. Returns
    {action, team, rationale}; action/team are '' when not chosen/invalid. Lazy; degrades
    to {} on error (→ deterministic policy stands). This is why no `invoke_vmaw` type is
    needed: 'escalate' IS the straight-to-VMAW route, chosen per defect."""
    adj = _Adjudicator(model_name=model_name, temperature=temperature)

    def triage_llm(*, defect: dict[str, Any], candidate_teams: list[str],
                   proposed_action: str | None = None) -> dict[str, Any]:
        cands = list(candidate_teams or [])
        prompt = (
            "You route a defect in a pathology-extraction self-correction loop. The "
            f"deterministic policy proposes action='{proposed_action}'. Choose the best "
            "CATALOG action:\n"
            "  re_extract_team | reprofile_block | renormalize_field | re_link | "
            "drop_and_flag | escalate\n"
            "Choose 'escalate' when RE-EXTRACTION/repair CAN'T fix it (e.g. the value is "
            "split across pages and needs context expansion, or it needs human judgment) "
            "— it routes straight to VMAW/SME with no wasted repair cycle. Otherwise keep "
            "a repair action. If the chosen action needs a team, pick one from the "
            "candidates; else leave team=''.\n\n"
            f"DEFECT: type={defect.get('defect_type')} ref={defect.get('target_ref')} "
            f"detail={defect.get('detail')}\n"
            f"CANDIDATE TEAMS: {cands}"
        )
        out = adj.call(prompt, _TriageRouteOut)
        if not out:
            return {}
        action = (out.action or "").strip()
        team = (out.team or "").strip()
        return {"action": action if action in _TRIAGE_ACTION_NAMES else "",
                "team": team if team in cands else "",
                "rationale": (out.rationale or "").strip()}

    return triage_llm
