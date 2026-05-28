"""
Repair catalog dispatcher (P3-M6) — executes the catalog actions triage selected.

`RepairExecutor.apply(state)` consumes `state["repair_requests"]` and performs each
action, mutating ONLY the targeted records (clean sections are never touched),
writing one `repair_log` entry per action (before/after/outcome — the audit trail),
incrementing `repair_budget_used`, and clearing `repair_requests` so the next
triage cycle recomputes from the re-verified envelope.

Catalog:
  re_extract_team   — focused, hinted re-extract of one team (reuses the
                      Extractor's existing re_extract_hints path via
                      SectionTeam.run(..., repair_hints=...)); the loop's
                      linker→verifiers→triage is the re-verify backstop.
  reprofile_block   — Block Profiler mis-routed a block: add the suggested section
                      to the block's hints, then re-extract the now-correct team.
  renormalize_field — re-run a normalization tool on a field (injected normalizers).
  re_link           — no team re-run; the repair→linker edge re-links. Logged only.
  drop_and_flag     — ungroundable record → drop it from the envelope + queue to SME.
  escalate          — non-addressable → queue to SME (triage usually routes these
                      straight to the queue, but kept here for safety).
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

_REF_RE = re.compile(r"([A-Za-z_]+)|\[(\d+)\]")


class RepairExecutor:
    def __init__(
        self,
        *,
        teams: dict[str, Any],
        normalizers: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self._teams = teams
        self._normalizers = normalizers or {}

    async def apply(self, state: dict[str, Any]) -> dict[str, Any]:
        requests = list(state.get("repair_requests") or [])
        section_outputs = dict(state.get("section_outputs") or {})
        team_results = dict(state.get("team_results") or {})
        doc_profile = copy.deepcopy(state.get("doc_profile") or {})
        repair_log = list(state.get("repair_log") or [])
        escalation_queue = list(state.get("escalation_queue") or [])
        relink_hints = list(state.get("relink_hints") or [])
        budget_used = int(state.get("repair_budget_used", 0) or 0)

        for req in requests:
            action = req.get("action")
            cycle = budget_used + 1
            before = _snapshot(section_outputs, req.get("section"))
            outcome = "noop"

            if action == "re_extract_team":
                outcome = await self._re_extract(req, state, section_outputs, team_results, doc_profile)
            elif action == "reprofile_block":
                outcome = await self._reprofile(req, state, section_outputs, team_results, doc_profile)
            elif action == "renormalize_field":
                outcome = self._renormalize(req, section_outputs)
            elif action == "re_link":
                # gap #3: carry the refuted pairing into the linker's "avoid/re-evaluate"
                # hints so the repair→linker re-run doesn't blindly re-emit it.
                relink_hints.append({"ref": req.get("target_ref"), "reason": req.get("detail")})
                outcome = "relink_pending"          # the repair→linker edge does the work
            elif action == "drop_and_flag":
                outcome = self._drop(req, section_outputs, escalation_queue)
            elif action == "escalate":
                escalation_queue.append({"section": req.get("section"), "ref": req.get("target_ref"),
                                         "kind": req.get("defect_type"), "detail": req.get("detail")})
                outcome = "escalated"
            else:
                outcome = f"unknown_action:{action}"

            repair_log.append({
                "cycle": cycle, "action": action, "team": req.get("team"),
                "section": req.get("section"), "target_ref": req.get("target_ref"),
                "defect_signature": req.get("signature"), "defect_type": req.get("defect_type"),
                "before": before, "after": _snapshot(section_outputs, req.get("section")),
                "outcome": outcome,
            })
            budget_used += 1

        return {
            "section_outputs": section_outputs, "team_results": team_results,
            "doc_profile": doc_profile, "repair_log": repair_log,
            "escalation_queue": escalation_queue, "repair_budget_used": budget_used,
            "relink_hints": relink_hints,     # gap #3: fed to the linker on re-link
            "repair_requests": [],            # cleared — next triage recomputes
        }

    # -- catalog actions -----------------------------------------------------

    async def _re_extract(self, req, state, section_outputs, team_results, doc_profile) -> str:
        team = self._teams.get(req.get("team"))
        if team is None:
            return "skipped_no_team"
        hints = [{
            "field_name": req.get("target_ref") or req.get("section") or "",
            "hint": (f"{req.get('detail','')}. FIX OR DROP — if you cannot ground it in a "
                     f"provided block, do NOT invent a citation. Candidate blocks: "
                     f"{','.join(req.get('candidate_block_ids') or []) or 'none'}"),
        }]
        scoped = dict(state)
        scoped["doc_profile"] = doc_profile
        try:
            res = await team.run(scoped, repair_hints=hints)
        except TypeError:
            res = await team.run(scoped)               # team predates repair_hints
        if res.output is not None:
            section_outputs[res.schema_section] = res.output
            team_results[req["team"]] = {
                "verdict": res.verdict,
                "llm_confidence_score": (res.output or {}).get("llm_confidence_score"),
                "needs_review_count": getattr(res, "needs_review_count", 0),
            }
            return "re_extracted"
        return "re_extract_empty"

    async def _reprofile(self, req, state, section_outputs, team_results, doc_profile) -> str:
        block_id = (req.get("candidate_block_ids") or [None])[0]
        suggested = req.get("section")
        changed = False
        for bp in (doc_profile.get("block_profiles") or []):
            if bp.get("block_id") == block_id:
                hints = list(bp.get("target_umbrella_hints") or [])
                if suggested and suggested not in hints:
                    hints.append(suggested)
                    bp["target_umbrella_hints"] = hints
                    changed = True
        # the now-correct team re-extracts with the reprofiled block visible
        await self._re_extract(req, state, section_outputs, team_results, doc_profile)
        return "reprofiled" if changed else "reprofile_no_block"

    def _renormalize(self, req, section_outputs) -> str:
        # detail = "{normalizer_key}: {input} → {canonical}" — key never contains ':'
        key = (req.get("detail") or "").split(":")[0].strip()
        tool = self._normalizers.get(key)
        if tool is None:
            return "skipped_no_normalizer"
        node, fkey = _resolve_parent(section_outputs, req.get("target_ref"))
        if node is None or fkey is None:
            return "ref_unresolved"
        try:
            raw = node[fkey]
        except (KeyError, IndexError, TypeError):
            return "ref_unresolved"
        try:
            res = tool(raw) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("renormalize failed: %s", exc)
            return "renormalize_error"
        canon = res.get("value")
        # WRITE the canonical ONLY when the normalizer matched a clean, different value;
        # the verbatim string stays in the record's occurrences (we never touch those).
        if res.get("matched") and canon not in (None, "") and str(canon) != str(raw):
            node[fkey] = canon
            return "renormalized"
        return "renormalize_no_match"

    def _drop(self, req, section_outputs, escalation_queue) -> str:
        node, key = _resolve_parent(section_outputs, req.get("target_ref"))
        dropped = None
        if isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
            dropped = node.pop(key)
        elif isinstance(node, dict) and key in node:
            dropped = node.pop(key)
        escalation_queue.append({"section": req.get("section"), "ref": req.get("target_ref"),
                                 "kind": "dropped_ungroundable", "detail": req.get("detail"),
                                 "dropped_record": dropped})
        return "dropped" if dropped is not None else "drop_ref_unresolved"


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
    """Walk to the PARENT container of the ref, returning (parent, last_key).
    `root` is section_outputs keyed by section; refs start with the section name."""
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


def _snapshot(section_outputs: dict[str, Any], section: str | None) -> Any:
    """Shallow audit snapshot of a section (truncated) for the repair ledger."""
    if not section:
        return None
    import json
    try:
        return json.loads(json.dumps(section_outputs.get(section), default=str))[:1] \
            if isinstance(section_outputs.get(section), list) else \
            {k: section_outputs.get(section, {}).get(k) for k in list((section_outputs.get(section) or {}))[:6]}
    except Exception:  # noqa: BLE001
        return None


def make_repair_node(*, executor: RepairExecutor):
    """LangGraph node — applies triage's repair_requests, then control returns to
    the linker (re-assemble) → verifiers (re-verify) → triage."""

    async def repair_node(state: dict[str, Any]) -> dict[str, Any]:
        delta = await executor.apply(state)
        logger.info("repair: doc_id=%s applied=%d budget_used=%d",
                    state.get("doc_id"), len(state.get("repair_requests") or []),
                    delta.get("repair_budget_used"))
        # Trace: one record per executed repair action, with the field's target_ref
        # so the field timeline shows when its team was re-extracted (or its field
        # renormalised, link re-evaluated, etc.).
        try:
            from core.trace_recorder import extend_trace, record
            recs: list[dict[str, Any]] = []
            requests = state.get("repair_requests") or []
            log = delta.get("repair_log") or state.get("repair_log") or []
            # repair_log is APPEND-style; the new entries are the tail of length len(requests).
            new_entries = log[-len(requests):] if requests else []
            _REPAIR_PLAIN = {
                "re_extract_team": "Our system re-ran the relevant team to take another pass.",
                "reprofile_block": "Our system re-classified a block of text and re-extracted from it.",
                "renormalize_field": "Our system normalised the field's value to its canonical form.",
                "re_link": "Our system asked the linker to reconsider an uncertain relationship.",
                "drop_and_flag": "Our system removed an ungrounded value and set it aside for review.",
            }
            for e in new_entries:
                act = e.get("action", "?")
                recs.append(record(
                    phase="repair", agent=f"RepairExecutor · {act}",
                    plain=_REPAIR_PLAIN.get(act, f"Our system applied an automatic '{act}' repair."),
                    section=e.get("section"), refs=[e.get("target_ref") or ""],
                    team=str(e.get("team") or ""),
                    input_summary=f"defect={e.get('defect_type','?')}",
                    output_summary=f"applied {act} (status={e.get('status','?')})",
                    verdict=str(e.get("status") or "applied"),
                    reasoning=str(e.get("detail") or "")))
            if recs:
                delta = {**delta, "agent_trace": extend_trace(state.get("agent_trace"), *recs)}
        except Exception:  # noqa: BLE001
            logger.exception("repair: trace recording failed (non-fatal)")
        return delta

    return repair_node
