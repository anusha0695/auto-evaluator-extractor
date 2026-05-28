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

# Chronology — the invoke order IS the order. Every record created via
# `core.trace_recorder.record` carries a monotonically-increasing `step` (assigned by
# `extend_trace`), so sorting by step alone reproduces the actual graph execution
# sequence. No phase-priority dict needed — phase is metadata, not an ordering hint.
#
# Legacy channels (scorecards, repair_log, vmaw_log, links, binding_items) don't have
# explicit steps. They're inserted with `_seq` = (last_agent_trace_step + position),
# so they appear AFTER the agent_trace records they're synthesised from. In a real
# run those channels are subsumed by agent_trace and never fire (see channel-skip
# guards in `assemble_field_trace`); they exist only as a back-compat shim for
# persisted v3 runs that pre-date the unified recorder.


def _step(phase: str, node: str, technical: str, plain: str, verdict: str = "",
          reasoning: str = "", kind: str = "", seq: int = 0,
          reasoning_scope: str = "field") -> dict[str, Any]:
    # reasoning_scope: "field" = the note is specifically about the selected field;
    # "section" = it's a section-level note (the agent didn't single this field out),
    # so the UI must NOT present it as if it explains THIS field.
    return {"phase": phase, "node": node, "technical": technical, "plain": plain,
            "verdict": verdict, "reasoning": (reasoning or "").strip(), "kind": kind,
            "reasoning_scope": reasoning_scope if (reasoning or "").strip() else "",
            "_seq": seq}


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

    # 1. all agent_trace records — extraction phase + planning, linking, dedup,
    # supersession, verification, triage, repair, vmaw, decision. Each record carries
    # its own `phase` set by the node that recorded it (core.trace_recorder.record),
    # and a monotonic `step` assigned by extend_trace — so sorting by step alone
    # reproduces the invoke order across all phases.
    # Filter logic:
    #   • record with a matching `section` → keep (team-scoped agent)
    #   • record whose `refs` touch the focused ref → keep (cross-section agent)
    #   • record without section/refs (e.g. Planner, DecisionRouter) → keep regardless,
    #     since their decision applies envelope-wide and is relevant to every field.
    last_step = -1
    trace_phases_present: set[str] = set()
    for i, a in enumerate(agent_trace or []):
        rec_section = a.get("section")
        rec_refs = a.get("refs") or []
        ref_touch = bool(ref) and any(_ref_tokens(rr) & _ref_tokens(ref) for rr in rec_refs if rr)
        section_match = (not section) or (rec_section == section) or (not rec_section and not rec_refs)
        if not (section_match or ref_touch):
            continue
        agent = a.get("agent", "agent")
        conf = a.get("confidence")
        phase_a = str(a.get("phase") or "extraction")
        trace_phases_present.add(phase_a)
        # per-field reasoning: extractor → the field's own rationale; auditor/arbiter
        # → the reason for THIS field if they singled it out, else the section note.
        # `scope` tells the UI whether the note is about THIS field ("field") or the
        # whole section ("section") — so a note about a *different* field (e.g. the
        # arbiter discussing VAF while you've selected biomarker_name) is labelled,
        # not silently shown as this field's "why".
        if agent.startswith("Extractor"):
            # Extractor sub-steps (thought / tool / tool-result) are scoped to the
            # specific field/section being extracted; their content (the thought text,
            # the tool args, the tool's JSON return) IS the per-field "why" for this
            # extraction. Mark them field-scope. The high-level Extractor record still
            # prefers a stored field_rationale when one exists; else its full reasoning
            # (the model's last thought) is treated as section-level.
            is_substep = ("·" in agent and agent != "Extractor (re-extract)")
            if field_rationale and not is_substep:
                why_text, scope = field_rationale, "field"
            elif is_substep:
                why_text, scope = a.get("reasoning", ""), "field"
            else:
                why_text, scope = a.get("reasoning", ""), "section"
        else:
            matched = _match_field_reason(a.get("field_reasons") or {}, leaf)
            if matched:
                why_text, scope = matched, "field"
            else:
                why_text, scope = a.get("reasoning", ""), "section"
        # JSON-only thoughts/results: the model dumped its proposed envelope as the
        # "thought" content (no natural-language reasoning). Surfacing that as the
        # field's "why" is misleading — replace with a structural summary.
        why_text, scope = _clean_json_reasoning(why_text, scope, a)
        step_n = int(a.get("step", i))
        last_step = max(last_step, step_n)
        # plain text: the recorder wrote it at record time (canonical source of truth).
        # For older persisted runs that pre-date the `plain` field, fall back to the
        # legacy regex-against-agent-name renderer so we don't regress those traces.
        plain_text = str(a.get("plain") or "").strip() or _plain_agent(a)
        steps.append(_step(
            phase_a, agent,
            technical=f"{agent}: {a.get('output_summary','')} · verdict={a.get('verdict','')}"
                      + (f" · conf={conf:.2f}" if isinstance(conf, (int, float)) else ""),
            plain=plain_text,
            verdict=a.get("verdict", ""), reasoning=why_text, reasoning_scope=scope,
            seq=step_n))

    # 1b. linking — back-compat for runs whose agent_trace has NO linking phase records
    # (legacy v3 path). In a current run, the linker_node has already emitted one
    # phase=linking record per emitted link into agent_trace, so this channel is
    # skipped to avoid duplicating those rows.
    nxt = last_step + 1
    if "linking" not in trace_phases_present:
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
                reasoning_scope="field", seq=nxt))
            nxt += 1

    # 2. verification — back-compat for runs whose agent_trace has NO verification
    # records (legacy v3 / v2 path). In a current run, verifier_node writes one
    # phase=verification record per scorecard into agent_trace.
    if "verification" not in trace_phases_present:
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
                reasoning_scope="field" if names_ref else "section", seq=nxt))
            nxt += 1

    # 3. repair — back-compat for legacy runs whose agent_trace lacks phase=repair.
    if "repair" not in trace_phases_present:
        for k, e in enumerate(repair_log or []):
            if not (e.get("target_ref") == ref or (section and e.get("section") == section)):
                continue
            act = e.get("action", "repair")
            steps.append(_step(
                "repair", f"repair · {act}",
                technical=f"cycle {e.get('cycle')}: {act} on {e.get('target_ref')} → {e.get('outcome')}",
                plain=_plain_repair(e),
                verdict=str(e.get("outcome", "")),
                reasoning_scope="field" if e.get("target_ref") == ref else "section", seq=nxt))
            nxt += 1

    # 3b. binding-verifier verdicts touching this field (toggle-gated in the UI).
    # The verifier_node also writes these as phase=verification "LinkBindingVerifier · X"
    # records; this channel is the back-compat shim for persisted runs whose
    # verification_v2 carries `binding_verifier.items` instead of trace records.
    if not any(str(x.get("agent","")).startswith("LinkBindingVerifier · ")
               for x in (agent_trace or [])):
        for q, v in enumerate(binding_items or []):
            if not _binding_matches(ref, v.get("ref")):
                continue
            chk = v.get("check", "")
            steps.append(_step(
                "verification", f"binding · {_BIND_CHECK.get(chk, chk)}",
                technical=f"binding {chk}: verdict={v.get('verdict')} · {(v.get('evidence') or '')[:160]}",
                plain=_plain_binding(v),
                verdict=str(v.get("verdict", "")), reasoning=str(v.get("evidence", "") or ""),
                kind="binding", seq=nxt))
            nxt += 1

    # 4. resolution — back-compat for runs whose agent_trace has no phase=vmaw records.
    if "vmaw" not in trace_phases_present and "resolution" not in trace_phases_present:
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
                verdict=str(res.get("status", "")), reasoning=str(res.get("rationale", "") or ""), seq=nxt))
            nxt += 1

    # Sort by the captured invoke order (step). Python's sorted() is stable, so two
    # records at the same step (e.g. the legacy gate constructs records by hand
    # without unique steps) keep their insertion order.
    steps.sort(key=lambda s: s["_seq"])
    for s in steps:
        s.pop("_seq", None)
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


def _looks_like_json(text: str) -> bool:
    """True iff `text` is essentially a JSON/code-fence dump rather than a sentence."""
    t = (text or "").strip()
    if not t:
        return False
    if t.startswith("```"):                     # fenced code/JSON block
        return True
    if t[:1] in ("{", "["):                     # raw JSON object or array
        # Defensive: require at least one structural delimiter further in.
        return any(c in t for c in (":", ","))
    return False


def _clean_json_reasoning(why: str, scope: str, a: dict[str, Any]) -> tuple[str, str]:
    """When the reasoning surfaces as a JSON/object dump (the model emitted its
    proposed envelope as a 'thought' or a tool returned raw JSON without a natural-
    language wrapper), replace it with a structural one-line summary so the SME isn't
    staring at a blob. Agent-specific:
      Extractor · thought   → "Model proposed output: N populated field(s)."
      Extractor · X → result → "Tool X returned a JSON result (toggle technical view for the full output)."
      anything else          → drop the dump (return empty string) — the agent's
                              `output_summary` is already a structural line."""
    if not _looks_like_json(why):
        return why, scope
    agent = str(a.get("agent") or "")
    out_summary = str(a.get("output_summary") or "").strip()
    # Number of populated fields if the dump is the team's proposed envelope.
    import re
    m = re.search(r'"\s*count[_a-zA-Z]*"\s*:\s*(\d+)', why)
    n = int(m.group(1)) if m else None
    if "thought" in agent.lower():
        # JSON-only "thought" = the model produced its structured answer in one
        # shot. The reasoning happened internally — it's attached to each
        # extracted value as a per-field rationale, not externalised as text.
        # Tell the SME where the reasoning IS, not that it "didn't happen".
        if n is not None:
            return (f"Model produced its structured answer in one step "
                    f"({n} populated field(s)) — reasoning lives in the "
                    f"per-field rationales."), scope
        return ("Model produced its structured answer in one step — "
                "reasoning lives in the per-field rationales."), scope
    if "→ result" in agent or agent.endswith("→ result"):
        return out_summary or "Tool returned a structured result.", "field"
    # Generic fall-through: replace the blob with the agent's structural summary.
    return out_summary, scope


def _plain_agent(a: dict[str, Any]) -> str:
    """Plain-language one-liner for any agent we record. Specific patterns first —
    `agent.startswith("Extractor")` would otherwise swallow every Extractor variant.
    Falls back to a generic line ONLY for unknown agents."""
    agent = a.get("agent") or ""
    verdict = a.get("verdict") or ""
    summary = a.get("output_summary") or ""

    # --- Extractor family ---
    if agent.startswith("Extractor · thought "):
        n = agent.rsplit("#", 1)[-1] if "#" in agent else ""
        return f"The model paused to think — step {n}." if n else "The model paused to think about what to do next."
    if agent.startswith("Extractor · tool · "):
        tool = agent.split("Extractor · tool · ", 1)[-1].strip()
        return f"The model asked the {tool} tool for help and waited for the answer."
    if " → result" in agent:
        tool = agent.replace("Extractor · ", "").replace(" → result", "").strip()
        return f"The {tool} tool returned its answer to the model."
    if agent.startswith("Extractor (re-extract)"):
        return "Our system took another careful pass at this section after a check raised a concern."
    if agent.startswith("Extractor"):
        return "Our system read this section of the report and pulled out the values it found."

    # --- Auditor / Arbiter ---
    if agent == "CoverageAuditor":
        return ("A second check compared what was pulled out against what the report seemed to contain"
                + (" and everything lined up." if verdict == "coverage_ok"
                   else " and noticed something might be missing."))
    if agent == "Arbiter":
        return "Because the two steps disagreed, a referee step decided how to proceed."

    # --- Preprocess ---
    if agent == "DocAIParser":
        return "Our system parsed the PDF into pages, blocks, and text we can reason over."
    if agent == "FaxHeaderFilter":
        return "Our system flagged fax-transport noise (headers/banners) so it's ignored downstream."
    if agent == "BlockProfiler":
        return "Our system labelled each block of text with a role and routed it to the right section(s)."
    if agent == "MedicalNER":
        return "Our system spotted clinical entities (genes, dates, IDs) in the source text and proposed candidates for each section."

    # --- Planner / DecisionRouter ---
    if agent == "Planner":
        return "Our system decided which extraction teams should run for this document."
    if agent == "DecisionRouter":
        return f"Our system made the final call on the whole document — verdict: {verdict or 'set'}."

    # --- Linker family ---
    if agent == "Linker":
        return ("Our system stitched the teams' outputs into a single record set, considered every "
                "kind of cross-section link, then ran dedup and supersession on top.")
    if agent.startswith("Linker · contextual_dropped"):
        return "The contextual link adjudicator proposed a link, but a deterministic check rejected it (so it wasn't committed)."
    if agent.startswith("Linker · Supersession"):
        return ("An addendum block referred back to an earlier finding — our system marked the finding for review "
                "as a possible amendment.")
    if agent.startswith("Linker · Dedup"):
        return ("The same entity appeared in two sections — our system kept the canonical owner's record and "
                "dropped the duplicate from the lower-priority section.")
    if agent.startswith("Linker · "):
        typ = agent.split("Linker · ", 1)[-1]
        return f"Our system linked two records as a {typ} relationship."

    # --- Verifiers ---
    if agent == "schema_validator":
        return ("A structural check confirmed the envelope's shape matches the expected schema."
                if verdict in ("passed", "") else "A structural check found a problem with the envelope's shape.")
    if agent == "coverage_audit":
        return "A coverage check compared what was extracted against the parser hypothesis."
    if agent == "link_consistency":
        return "A check made sure every cross-section link points at records that actually exist."
    if agent == "evidence_confidence":
        return "A check made sure each finding has at least one cited block of evidence."
    if agent == "recall_floor":
        return "A check looked for fields the block-role mapping says should be present but came back empty."
    if agent == "attribution":
        return "A check made sure each attribute really describes its owner record (not a neighbour)."
    if agent == "normalization":
        return "A check looked for fields whose canonical form differs from what was extracted."
    if agent == "hgvs_validity":
        return "A check confirmed every HGVS change is structurally valid."
    if agent == "LinkBindingVerifier":
        return "A binding check confirmed the relationship each link claims is supported by its cited evidence."
    if agent.startswith("LinkBindingVerifier · "):
        which = agent.split("LinkBindingVerifier · ", 1)[-1]
        return f"A {which} binding check ran on this item against its cited evidence."

    # --- Triage / Repair / VMAW ---
    if agent == "Triage":
        return ("Our system classified the open defects and routed each one — either back for an "
                "automatic repair, or up to a human reviewer.")
    if agent.startswith("Triage · repair · "):
        act = agent.split("Triage · repair · ", 1)[-1]
        return f"Triage queued an automatic '{act}' repair for this field."
    if agent.startswith("Triage · escalate · "):
        kind = agent.split("Triage · escalate · ", 1)[-1]
        return f"Triage flagged this '{kind}' for a human reviewer."
    if agent.startswith("RepairExecutor · "):
        act = agent.split("RepairExecutor · ", 1)[-1]
        plain_map = {
            "re_extract_team": "Our system re-ran the relevant team to take another pass.",
            "reprofile_block": "Our system re-classified a block of text and re-extracted from it.",
            "renormalize_field": "Our system normalised the field's value to its canonical form.",
            "re_link": "Our system asked the linker to reconsider an uncertain relationship.",
            "drop_and_flag": "Our system removed an ungrounded value and set it aside for review.",
        }
        return plain_map.get(act, f"Our system applied an automatic '{act}' repair.")
    if agent.startswith("VMAW · "):
        cap = agent.split("VMAW · ", 1)[-1]
        plain_map = {
            "EC": "VMAW expanded the context window to look at more text around the disputed value.",
            "CITE": "VMAW hunted for a citation that supports (or refutes) the disputed value.",
            "VA": "VMAW adjudicated between conflicting candidate values for the same field.",
        }
        return plain_map.get(cap, f"VMAW ran '{cap}' on this item.")

    # Final fallback (genuinely unknown agent).
    return summary or "A processing step ran on this section."


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
