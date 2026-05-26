"""
Field-centric trace + plain-language explanation (P3-M8c).

Two pure capabilities the SME review screen renders (no Streamlit import here, so
the gate can exercise it offline):

  assemble_field_trace(...) — merge the agent_trace, verifier scorecards, repair_log
    and vmaw_log into ONE ordered timeline filtered to a single field/record:
    extraction → verification → repair cycles → VMAW resolution. Each step carries
    BOTH a technical line (agent/node/verdict/refs) and a plain line (no jargon), so
    the UI's technical/SME toggle just picks a key.

  explain(item, mode) — the headline "what's the confusion / what we need you to
    decide" for an escalation item. Deterministic templates keyed by defect kind,
    slotting the case specifics (extractor value, VMAW proposal, citation). `mode`
    is "plain" (SME) or "technical".

Plain copy rule: never surface internal vocabulary (kind codes, node names, refs,
"sme_flag", "binding_refuted"); say what happened in clinical-reviewer terms.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN_RE = re.compile(r"[A-Za-z_]+\[\d+\]")

# escalation kind → plain-language frame. {proposal} is appended separately.
_PLAIN: dict[str, str] = {
    "needs_review": (
        "Our system had to interpret a value that wasn't perfectly clear in the "
        "report (often an OCR or formatting issue). It has a reading, but couldn't "
        "fully confirm it against the source — please check it matches the document."),
    "binding_refuted": (
        "Our system found this value in the report but isn't sure it belongs to "
        "this test — it may actually describe a different one nearby. More than one "
        "reading is possible, so we'd like you to confirm which is correct."),
    "binding_uncertain": (
        "The wording in the report wasn't clear enough for our system to confirm "
        "this value with confidence. Please check the highlighted text and confirm."),
    "recall_floor_loud": (
        "Our system expected a value here — the part of the report that usually "
        "contains it is present — but it didn't capture one. Please check whether "
        "the report actually states it."),
    "recall_miss_present": (
        "Our system expected a value here but didn't capture one. Please check "
        "whether the report states it."),
    "dropped_ungroundable": (
        "Our system couldn't find supporting text for this value, so it left it out. "
        "Please confirm it should be absent — or restore it if the report does state it."),
    "schema_error": (
        "This value didn't come back in the expected format, so our system couldn't "
        "use it as-is. Please review and correct it."),
    "attribution_contested": (
        "Our system found this value but isn't sure it describes THIS specimen/record — "
        "several similar records appear together and the value may belong to a "
        "neighbouring one. Please confirm which record it describes."),
}
_PLAIN_DEFAULT = "Our system flagged this for your review and couldn't settle it on its own."

# "why did we flag / possibly miss this?" — the reason behind the flag, in
# reviewer terms. Shown as a separate callout from the headline explanation.
_WHY: dict[str, str] = {
    "binding_refuted": (
        "The value and the test it describes sit close together in the report "
        "(often the same table row), so it's unclear which test the value belongs "
        "to. The automatic check couldn't confirm the pairing — it may have been "
        "attached to the wrong test."),
    "binding_uncertain": (
        "The wording around this value was ambiguous, so the automatic check "
        "couldn't confirm it links to this test."),
    "needs_review": (
        "A value had to be interpreted (an OCR or formatting issue), so the system "
        "flagged it rather than trust the guess."),
    "recall_floor_loud": (
        "This kind of value is usually present in this section of the report, but "
        "none was captured — it may have been overlooked, or genuinely absent."),
    "recall_miss_present": (
        "A value was expected here but none was captured — it may have been overlooked."),
    "dropped_ungroundable": (
        "No supporting text could be found for this value, so it was removed pending "
        "your check."),
    "attribution_contested": (
        "More than one record of the same kind (e.g. several specimens) appears "
        "together, so it's unclear which one this value describes; the automatic "
        "check couldn't confirm the pairing, and it may be attached to the wrong record."),
}
_WHY_DEFAULT = "The automatic checks couldn't confirm this with enough confidence to finalize it."


def why(item: dict[str, Any], *, mode: str = "plain") -> str:
    """The 'why we flagged / may have missed this' reason. For links this is the
    binding ambiguity; for other defects, the relevant cause. mode = plain|technical."""
    kind = item.get("kind") or ""
    if mode == "technical":
        return (f"defect={kind} · ref={item.get('ref')} · {item.get('detail') or '(no detail)'}")
    return _WHY.get(kind, _WHY_DEFAULT)

# phase ordering for the merged timeline
_PHASE_ORDER = {"extraction": 0, "linking": 1, "verification": 2, "repair": 3,
                "resolution": 4}


def _step(phase: str, node: str, technical: str, plain: str, verdict: str = "",
          reasoning: str = "", kind: str = "", seq: int = 0,
          reasoning_scope: str = "field") -> dict[str, Any]:
    # reasoning_scope: "field" = the note is specifically about the selected field;
    # "section" = it's a section-level note (the agent didn't single this field out),
    # so the UI must NOT present it as if it explains THIS field.
    return {"phase": phase, "node": node, "technical": technical, "plain": plain,
            "verdict": verdict, "reasoning": (reasoning or "").strip(), "kind": kind,
            "reasoning_scope": reasoning_scope if (reasoning or "").strip() else "",
            "_order": (_PHASE_ORDER.get(phase, 9), seq)}


def _ref_tokens(ref: str | None) -> set[str]:
    return set(_TOKEN_RE.findall(str(ref or "")))


def _leaf(ref: str | None) -> str:
    """Last field name in a ref: 'a.b[0].result' → 'result'."""
    return str(ref or "").split(".")[-1].split("[")[0]


def _match_field_reason(field_reasons: dict[str, str], leaf: str) -> str:
    """Pick the per-field reason whose field_name matches the selected leaf
    (case-insensitive, either-contains — agent field names are fuzzy)."""
    if not field_reasons or not leaf:
        return ""
    ll = leaf.lower()
    for k, v in field_reasons.items():
        kl = str(k).lower()
        if ll == kl or ll in kl or kl in ll:
            return str(v)
    return ""


def _binding_matches(field_ref: str | None, binding_ref: str | None) -> bool:
    """A binding verdict touches the field if they share an array token like
    `other_molecular_biomarkers[0]` (binding refs are section-relative)."""
    ft, bt = _ref_tokens(field_ref), _ref_tokens(binding_ref)
    return bool(ft and bt and (ft & bt))


_BIND_CHECK = {"V1": "grounding", "V2": "relationship", "V3": "hallucination", "V4": "link"}


def _plain_binding(v: dict[str, Any]) -> str:
    verdict = (v.get("verdict") or "").lower()
    what = {"V1": "whether the value is supported by the cited text",
            "V2": "whether this result belongs to this test (not a neighbouring one)",
            "V3": "whether this value actually appears in the report",
            "V4": "whether this cross-reference holds"}.get(v.get("check"), "this value")
    if verdict == "refuted":
        return f"An evidence check on {what} found it was NOT supported by the source."
    if verdict == "uncertain":
        return f"An evidence check on {what} couldn't confirm it either way."
    return f"An evidence check reviewed {what}."


def _link_side_touches(side: str | None, ref: str | None) -> bool:
    """A link endpoint `side` touches the selected `ref` if either ref is the
    other (record-level link shows on its sub-fields, and vice-versa)."""
    if not side or not ref:
        return False
    s, r = str(side), str(ref)
    return (s == r or r.startswith(s + ".") or r.startswith(s + "[")
            or s.startswith(r + ".") or s.startswith(r + "["))


def _plain_link(lk: dict[str, Any], *, other_ref: str) -> str:
    """Plain-language rendering of a linker relationship for the selected field."""
    typ = str(lk.get("type") or "related").replace("_", " ")
    method = str(lk.get("method") or "")
    how = "by a deterministic rule" if method == "deterministic" else (
        "after an LLM relationship check" if method else "")
    other = _leaf(other_ref) or other_ref
    base = f"The linker connected this to “{other}” as a {typ} relationship"
    return (base + (f" {how}." if how else ".")).strip()


def assemble_field_trace(
    *,
    ref: str | None,
    section: str | None,
    agent_trace: list[dict[str, Any]] | None = None,
    scorecards: list[dict[str, Any]] | None = None,
    repair_log: list[dict[str, Any]] | None = None,
    vmaw_log: list[dict[str, Any]] | None = None,
    binding_items: list[dict[str, Any]] | None = None,
    links: list[dict[str, Any]] | None = None,
    field_rationale: str = "",
) -> list[dict[str, Any]]:
    """One ordered, field-filtered timeline. `ref` is the dotted record/field ref;
    `section` scopes the extraction + verification steps. Per-field reasoning: the
    Extractor step shows `field_rationale` (the field's own stored rationale); the
    Auditor/Arbiter steps show the reason for THIS field if they singled it out
    (matched by name), else their section-level note. Linker relationships touching
    the ref appear as `phase="linking"` steps. Binding verdicts that touch the field
    are added as `kind="binding"` steps (UI-toggle-gated)."""
    steps: list[dict[str, Any]] = []
    leaf = _leaf(ref)

    # 1. extraction — the team's agents for this section
    for i, a in enumerate(agent_trace or []):
        if section and a.get("section") != section:
            continue
        agent = a.get("agent", "agent")
        conf = a.get("confidence")
        # per-field reasoning: extractor → the field's own rationale; auditor/arbiter
        # → the reason for THIS field if they singled it out, else the section note.
        # `scope` tells the UI whether the note is about THIS field ("field") or the
        # whole section ("section") — so a note about a *different* field (e.g. the
        # arbiter discussing VAF while you've selected biomarker_name) is labelled,
        # not silently shown as this field's "why".
        if agent.startswith("Extractor"):
            if field_rationale:
                why, scope = field_rationale, "field"
            else:
                why, scope = a.get("reasoning", ""), "section"
        else:
            matched = _match_field_reason(a.get("field_reasons") or {}, leaf)
            if matched:
                why, scope = matched, "field"
            else:
                why, scope = a.get("reasoning", ""), "section"
        steps.append(_step(
            "extraction", agent,
            technical=f"{agent}: {a.get('output_summary','')} · verdict={a.get('verdict','')}"
                      + (f" · conf={conf:.2f}" if isinstance(conf, (int, float)) else ""),
            plain=_plain_agent(a),
            verdict=a.get("verdict", ""), reasoning=why, reasoning_scope=scope,
            seq=a.get("step", i)))

    # 1b. linking — relationships the Linker formed that touch THIS ref. These live
    # in verification_v2.links; they're shown as their own phase (between extraction
    # and verification) so the timeline explains *why two fields are connected*, not
    # just that they're highlighted together. Each link's `rationale` is the per-link
    # "why" (e.g. "Same HGNC-normalized gene 'JAK2'.").
    for p, lk in enumerate(links or []):
        if not isinstance(lk, dict):
            continue
        a_ref, b_ref = lk.get("from_ref"), lk.get("to_ref")
        if _link_side_touches(a_ref, ref):
            other = b_ref
        elif _link_side_touches(b_ref, ref):
            other = a_ref
        else:
            continue
        typ = str(lk.get("type") or "related")
        conf = lk.get("confidence")
        steps.append(_step(
            "linking", f"Linker · {typ}",
            technical=f"link {typ}: {a_ref} ↔ {b_ref} · method={lk.get('method','')}"
                      + (f" · conf={conf:.2f}" if isinstance(conf, (int, float)) else ""),
            plain=_plain_link(lk, other_ref=str(other or "")),
            verdict=str(lk.get("method") or "linked"),
            reasoning=str(lk.get("rationale", "") or ""),
            reasoning_scope="field", seq=p))

    # 2. verification — scorecards that failed or name this ref
    for j, s in enumerate(scorecards or []):
        names_ref = _scorecard_touches(s, ref)
        if not ((not s.get("passed", True)) or names_ref):
            continue
        name = s.get("verifier_name", "verifier")
        steps.append(_step(
            "verification", name,
            technical=f"{name}: passed={s.get('passed')} · {s.get('notes','')[:120]}",
            plain=_plain_verifier(name, s),
            verdict="passed" if s.get("passed") else "flagged",
            reasoning=str(s.get("notes", "") or ""),
            reasoning_scope="field" if names_ref else "section", seq=j))

    # 3. repair — repair_log entries touching the ref / section
    for k, e in enumerate(repair_log or []):
        if not (e.get("target_ref") == ref or (section and e.get("section") == section)):
            continue
        act = e.get("action", "repair")
        steps.append(_step(
            "repair", f"repair · {act}",
            technical=f"cycle {e.get('cycle')}: {act} on {e.get('target_ref')} → {e.get('outcome')}",
            plain=_plain_repair(e),
            verdict=str(e.get("outcome", "")),
            reasoning_scope="field" if e.get("target_ref") == ref else "section", seq=k))

    # 3b. binding-verifier verdicts touching this field (toggle-gated in the UI)
    for q, v in enumerate(binding_items or []):
        if not _binding_matches(ref, v.get("ref")):
            continue
        chk = v.get("check", "")
        steps.append(_step(
            "verification", f"binding · {_BIND_CHECK.get(chk, chk)}",
            technical=f"binding {chk}: verdict={v.get('verdict')} · {(v.get('evidence') or '')[:160]}",
            plain=_plain_binding(v),
            verdict=str(v.get("verdict", "")), reasoning=str(v.get("evidence", "") or ""),
            kind="binding", seq=100 + q))

    # 4. resolution — VMAW log entries for this ref
    for m, v in enumerate(vmaw_log or []):
        res = v.get("resolution") or {}
        if res.get("item_ref") != ref:
            continue
        cap = res.get("capability") or "VMAW"
        steps.append(_step(
            "resolution", f"VMAW · {cap}",
            technical=f"VMAW({cap}): status={res.get('status')} grounded={res.get('grounded')} "
                      f"contested={res.get('contested')} → {res.get('resolved_value')}",
            plain=_plain_vmaw(res),
            verdict=str(res.get("status", "")), reasoning=str(res.get("rationale", "") or ""), seq=m))

    steps.sort(key=lambda s: s["_order"])
    for s in steps:
        s.pop("_order", None)
    return steps


def explain(item: dict[str, Any], *, mode: str = "plain") -> str:
    """Headline explanation for an escalation item. mode = 'plain' | 'technical'."""
    kind = item.get("kind") or ""
    if mode == "technical":
        prop = item.get("vmaw_proposal") or {}
        bits = [f"kind={kind}", f"ref={item.get('ref')}", f"section={item.get('section')}"]
        if item.get("detail"):
            bits.append(f"detail={item['detail']}")
        if prop:
            bits.append(f"vmaw_proposal={prop.get('value')} grounded={prop.get('grounded')} "
                        f"contested={prop.get('contested')} cite={prop.get('citation_block_ids')}")
        return " · ".join(str(b) for b in bits)

    base = _PLAIN.get(kind, _PLAIN_DEFAULT)
    prop = item.get("vmaw_proposal") or {}
    if prop.get("value") is not None:
        cite = ", ".join(prop.get("citation_block_ids") or []) or "the highlighted text"
        base += (f" Our system's suggestion is “{prop['value']}” (from {cite}). "
                 "You can approve it, edit it, or keep it flagged.")
    return base


# -- plain renderers per phase ---------------------------------------------


def _plain_agent(a: dict[str, Any]) -> str:
    agent = a.get("agent", "")
    if agent.startswith("Extractor (re-extract)"):
        return "Our system took another careful pass at this section after a check raised a concern."
    if agent.startswith("Extractor"):
        return "Our system first read this section of the report and pulled out the values it found."
    if agent == "CoverageAuditor":
        return ("A second check compared what was pulled out against what the report seemed to contain"
                + (" and everything lined up." if a.get("verdict") == "coverage_ok"
                   else " and noticed something might be missing."))
    if agent == "Arbiter":
        return "Because the two steps disagreed, a referee step decided how to proceed."
    return "A processing step ran on this section."


def _plain_verifier(name: str, s: dict[str, Any]) -> str:
    if s.get("passed", True):
        return "An automated check reviewed this and found no problem."
    return "An automated check reviewed this and flagged something that needed a closer look."


def _plain_repair(e: dict[str, Any]) -> str:
    act = e.get("action")
    if act == "re_extract_team":
        return "Our system automatically re-read the relevant part of the report to try to fix the issue."
    if act == "drop_and_flag":
        return "Our system removed a value it couldn't support and set it aside for your review."
    if act == "reprofile_block":
        return "Our system realised it had been looking in the wrong place and re-checked the right section."
    return "Our system attempted an automatic fix."


def _plain_vmaw(res: dict[str, Any]) -> str:
    status = res.get("status")
    if status == "auto_applied":
        return "Our system found clear supporting text in the report and confirmed this automatically."
    if status == "proposed_for_sme":
        return ("Our system has a suggested answer but wants you to confirm it, because the choice "
                "isn't certain enough to decide on its own.")
    return "Our system looked deeper but still couldn't settle this — it needs your judgment."


def _scorecard_touches(s: dict[str, Any], ref: str | None) -> bool:
    if not ref:
        return False
    # live scorecards carry `field_errors` [{field_name|loc}]; the persisted
    # verification_v2 artifact flattens these to `error_locs` (list of loc strings).
    for e in (s.get("field_errors") or []):
        loc = (e.get("field_name") or e.get("loc") or e.get("ref") or "") if isinstance(e, dict) else str(e)
        if loc and (ref in str(loc) or str(loc) in ref):
            return True
    for loc in (s.get("error_locs") or []):
        if ref in str(loc) or str(loc) in ref:
            return True
    return False
