"""
ui/phase1/field_view — Option C field view + pipeline flowchart.

`render_field_flow_html(steps, *, ref, technical, show_binding)` returns the HTML for
the per-field stepped list (one row per agent invocation that touched the focused
field, grouped by phase, with inline reasoning). Designed to consume the output of
`ui.phase1.field_trace.assemble_field_trace` — same step dicts the previous
`evidence.render_trace` consumed — so it can drop into the existing call sites
without changing the trace assembler.

`pipeline_flow_svg()` returns the SVG string for the pipeline flowchart with the
triage repair loopback + VMAW escalation branch.

Pure (returns strings) — testable offline; no Streamlit import. Callers do
`st.markdown(html, unsafe_allow_html=True)` (or `st.html(html)` on newer Streamlit).
All styles inlined (Streamlit sanitises `<style>` blocks in markdown).
"""

from __future__ import annotations

import html
from typing import Any

# Phase visual chip (background, text color, label).
_PHASE_CHIPS: dict[str, tuple[str, str, str]] = {
    "preprocess":   ("#F1EFE8", "#444441", "preprocess"),
    "planning":     ("#F1EFE8", "#444441", "planning"),
    "extraction":   ("#E6F1FB", "#0C447C", "extraction"),
    "linking":      ("#EEEDFE", "#3C3489", "linking"),
    "supersession": ("#EEEDFE", "#3C3489", "supersession"),
    "dedup":        ("#EEEDFE", "#3C3489", "dedup"),
    "verification": ("#E1F5EE", "#085041", "verification"),
    "triage":       ("#FAEEDA", "#633806", "triage"),
    "repair":       ("#FAEEDA", "#633806", "repair"),
    "vmaw":         ("#FAECE7", "#712B13", "vmaw"),
    "decision":     ("#E6F1FB", "#0C447C", "decision"),
}

# Verdict → pill (background, text color). Falls back to neutral when unknown.
_VERDICT_PILLS: dict[str, tuple[str, str]] = {
    "passed":      ("#EAF3DE", "#27500A"),
    "accept":      ("#EAF3DE", "#27500A"),
    "coverage_ok": ("#EAF3DE", "#27500A"),
    "extracted":   ("#EAF3DE", "#27500A"),
    "re_extracted": ("#EAF3DE", "#27500A"),
    "tool_result": ("#EAF3DE", "#27500A"),
    "applied":     ("#E6F1FB", "#0C447C"),
    "linked":      ("#E6F1FB", "#0C447C"),
    "deterministic": ("#E6F1FB", "#0C447C"),
    "contextual":  ("#E6F1FB", "#0C447C"),
    "tool_called": ("#E6F1FB", "#0C447C"),
    "thinking":    ("#E6F1FB", "#0C447C"),
    "assembled":   ("#E6F1FB", "#0C447C"),
    "profiled":    ("#F1EFE8", "#444441"),
    "filtered":    ("#F1EFE8", "#444441"),
    "parsed":      ("#F1EFE8", "#444441"),
    "planned":     ("#F1EFE8", "#444441"),
    "uncertain":   ("#FAEEDA", "#633806"),
    "gap_flagged": ("#FAEEDA", "#633806"),
    "RE_EXTRACT":  ("#FAEEDA", "#633806"),
    "repair":      ("#FAEEDA", "#633806"),
    "needs_review": ("#FAEEDA", "#633806"),
    "dropped":     ("#FAEEDA", "#633806"),
    "escalated":   ("#FAECE7", "#712B13"),
    "refuted":     ("#FCEBEB", "#791F1F"),
    "failed":      ("#FCEBEB", "#791F1F"),
    "sme_flag":    ("#FCEBEB", "#791F1F"),
}


def _esc(x: Any) -> str:
    return html.escape(str(x or ""))


def _phase_chip(phase: str) -> str:
    bg, fg, label = _PHASE_CHIPS.get(phase, ("#F1EFE8", "#444441", phase or "step"))
    return (f'<span style="display:inline-block;font-size:11px;padding:2px 8px;'
            f'border-radius:99px;background:{bg};color:{fg};font-weight:500;">'
            f'{_esc(label)}</span>')


def _verdict_pill(verdict: str) -> str:
    if not verdict:
        return ""
    bg, fg = _VERDICT_PILLS.get(verdict, ("#F1EFE8", "#444441"))
    return (f'<span style="font-size:11px;padding:1px 7px;border-radius:4px;'
            f'background:{bg};color:{fg};">{_esc(verdict)}</span>')


def _cycle_band(cycle_n: int, budget_used: int | None = None) -> str:
    info = f"cycle {cycle_n}"
    if budget_used is not None:
        info += f" · budget {budget_used} used · recur-guard armed"
    return (f'<div style="background:#FAEEDA;color:#633806;padding:6px 14px;'
            f'font-size:12px;font-weight:500;display:flex;align-items:center;gap:8px;'
            f'border-top:0.5px solid rgba(0,0,0,0.06);border-bottom:0.5px solid rgba(0,0,0,0.06);">'
            f'<span>↻</span><span>Repair {_esc(info)}</span></div>')


def _row(step: dict[str, Any], technical: bool) -> str:
    phase = str(step.get("phase") or "")
    node = step.get("node") or step.get("agent") or "agent"
    line = step.get("technical") if technical else step.get("plain")
    if not line:
        line = step.get("output_summary") or ""
    reasoning = (step.get("reasoning") or "").strip()
    section_reasoning = (step.get("section_reasoning") or "").strip()
    scope = step.get("reasoning_scope")
    verdict = step.get("verdict") or ""

    def _sub(label: str, text: str) -> str:
        return (
            f'<div style="color:var(--color-text-secondary);font-size:12px;'
            f'margin-top:3px;line-height:1.4;">'
            f'<span style="color:var(--color-text-tertiary,#7a7a76);font-weight:500;'
            f'margin-right:6px;">↳ {_esc(label)}</span>'
            f'<span style="white-space:pre-wrap;">{_esc(text)}</span>'
            f'</div>'
        )

    # Compose up to two reasoning lines:
    #   • primary `reasoning`: the field-specific 'why' when scope=field, OR the
    #     section-level note when scope=section. Labelled accordingly.
    #   • `section_reasoning`: the model's own section-level prose (its <reasoning>
    #     block / full final message), only present on the high-level Extractor row
    #     when distinct from the per-field rationale. Always labelled "section
    #     reasoning" so the SME knows it's NOT about this specific field.
    sub = ""
    if reasoning:
        label = "why (this field)" if scope in ("field", None) else \
                "note (section-level — not specific to this field)"
        sub += _sub(label, reasoning)
    if section_reasoning:
        sub += _sub("section reasoning (what the model thought about the whole section)",
                    section_reasoning)
    return (
        '<div style="display:grid;grid-template-columns:110px 1fr 110px;padding:10px 14px;'
        'align-items:flex-start;gap:10px;font-size:13px;'
        'border-bottom:0.5px solid rgba(0,0,0,0.06);">'
        f'<span style="justify-self:start;">{_phase_chip(phase)}</span>'
        f'<div><span style="font-weight:500;">{_esc(node)}</span> '
        f'<span style="color:var(--color-text-secondary);">{_esc(line)}</span>{sub}</div>'
        f'<span style="justify-self:end;">{_verdict_pill(verdict)}</span>'
        "</div>"
    )


def _field_header(ref: str | None, value: Any = None) -> str:
    ref_txt = _esc(ref or "")
    val_txt = "" if value in (None, "") else (
        f'<span style="font-weight:500;">{_esc(value)}</span>')
    return (
        '<div style="display:flex;align-items:center;gap:10px;'
        'background:var(--color-background-secondary);border-radius:8px;'
        'padding:12px 14px;margin-bottom:12px;font-size:13px;">'
        '<span style="color:var(--color-text-secondary);" aria-hidden="true">◎</span>'
        f'<span style="font-family:var(--font-mono);color:var(--color-text-secondary);">{ref_txt}</span>'
        f'<span style="flex:1"></span>{val_txt}'
        "</div>"
    )


def render_field_flow_html(
    steps: list[dict[str, Any]],
    *,
    ref: str | None = None,
    value: Any = None,
    technical: bool = True,
    show_binding: bool = False,
) -> str:
    """Render the field-flow card (header + per-step rows + repair cycle bands).

    `steps` is the output of `ui.phase1.field_trace.assemble_field_trace` — the same
    shape `evidence.render_trace` used. Each step has: phase, node, technical, plain,
    reasoning, reasoning_scope, verdict, kind (optional). `kind == "binding"` rows
    are gated by `show_binding`.

    Returns a self-contained HTML string. No Streamlit import."""
    if not steps:
        return ('<div style="font-size:13px;color:var(--color-text-secondary);'
                'padding:10px 0;">No recorded processing steps for this field.</div>')

    visible = [s for s in steps if show_binding or s.get("kind") != "binding"]
    if not visible:
        return ('<div style="font-size:13px;color:var(--color-text-secondary);'
                'padding:10px 0;">No visible steps (binding-only — toggle to show).</div>')

    parts: list[str] = [_field_header(ref, value),
                        '<div style="background:var(--color-background-primary);'
                        'border:0.5px solid rgba(0,0,0,0.1);border-radius:12px;overflow:hidden;">']

    # Insert "Repair cycle N" bands when consecutive runs of repair/extraction follow
    # a triage decision in the same field's timeline. We count "cycles" by counting
    # the number of `phase == "repair"` rows seen and emit a band before each.
    cycle_n = 0
    last_phase = ""
    for s in visible:
        ph = str(s.get("phase") or "")
        if ph == "repair" and last_phase != "repair":
            cycle_n += 1
            parts.append(_cycle_band(cycle_n + 1))   # cycle 1 == initial; banner shows cycle 2+
        parts.append(_row(s, technical))
        last_phase = ph

    parts.append("</div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Pipeline flowchart — with the triage repair loopback + VMAW branch.
# Composed from a per-phase node dict + per-edge arrow dict so the chart can be
# rendered field-aware (only the phases that actually fired) by passing
# `active_phases` into `pipeline_flow_svg`.
# ---------------------------------------------------------------------------


_SVG_DEFS = """\
  <defs>
    <marker id="ar-n" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="#5F5E5A"/>
    </marker>
    <marker id="ar-a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="#BA7517"/>
    </marker>
    <marker id="ar-c" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="#D85A30"/>
    </marker>
  </defs>
"""

# Per-phase node SVG. Keys are the canonical phase names that appear in
# `record.phase` so a field's `{phase for s in steps for phase}` set maps directly.
_NODE_SVG: dict[str, str] = {
    "preprocess": (
        '<rect x="10"  y="40" width="100" height="44" rx="6" fill="#F1EFE8" stroke="#5F5E5A" stroke-width="0.5"/>'
        '<text x="60"  y="58" text-anchor="middle" font-size="12" font-weight="500" fill="#2C2C2A">preprocess</text>'
        '<text x="60"  y="74" text-anchor="middle" font-size="10" fill="#5F5E5A">DocAI · NER · profile</text>'
    ),
    "planning": (
        '<rect x="130" y="40" width="80"  height="44" rx="6" fill="#F1EFE8" stroke="#5F5E5A" stroke-width="0.5"/>'
        '<text x="170" y="58" text-anchor="middle" font-size="12" font-weight="500" fill="#2C2C2A">planner</text>'
        '<text x="170" y="74" text-anchor="middle" font-size="10" fill="#5F5E5A">active teams</text>'
    ),
    "extraction": (
        '<rect x="230" y="40" width="140" height="44" rx="6" fill="#E6F1FB" stroke="#185FA5" stroke-width="0.5"/>'
        '<text x="300" y="58" text-anchor="middle" font-size="12" font-weight="500" fill="#0C447C">teams (parallel)</text>'
        '<text x="300" y="74" text-anchor="middle" font-size="10" fill="#185FA5">extract · audit · arbiter</text>'
    ),
    "linking": (
        '<rect x="390" y="40" width="100" height="44" rx="6" fill="#EEEDFE" stroke="#534AB7" stroke-width="0.5"/>'
        '<text x="440" y="58" text-anchor="middle" font-size="12" font-weight="500" fill="#3C3489">linker</text>'
        '<text x="440" y="74" text-anchor="middle" font-size="10" fill="#534AB7">assemble · dedup</text>'
    ),
    "verification": (
        '<rect x="510" y="40" width="150" height="44" rx="6" fill="#E1F5EE" stroke="#0F6E56" stroke-width="0.5"/>'
        '<text x="585" y="58" text-anchor="middle" font-size="12" font-weight="500" fill="#085041">verifiers (parallel)</text>'
        '<text x="585" y="74" text-anchor="middle" font-size="10" fill="#0F6E56">schema · recall · binding</text>'
    ),
    "triage": (
        '<rect x="510" y="155" width="150" height="44" rx="6" fill="#FAEEDA" stroke="#BA7517" stroke-width="0.5"/>'
        '<text x="585" y="173" text-anchor="middle" font-size="12" font-weight="500" fill="#633806">triage</text>'
        '<text x="585" y="189" text-anchor="middle" font-size="10" fill="#854F0B">classify defects · route</text>'
    ),
    "repair": (
        '<rect x="240" y="248" width="140" height="44" rx="6" fill="#FAEEDA" stroke="#BA7517" stroke-width="0.5"/>'
        '<text x="310" y="266" text-anchor="middle" font-size="12" font-weight="500" fill="#633806">repair</text>'
        '<text x="310" y="282" text-anchor="middle" font-size="10" fill="#854F0B">re_extract · re_link · …</text>'
    ),
    "vmaw": (
        '<rect x="510" y="248" width="150" height="44" rx="6" fill="#FAECE7" stroke="#993C1D" stroke-width="0.5"/>'
        '<text x="585" y="266" text-anchor="middle" font-size="12" font-weight="500" fill="#712B13">vmaw</text>'
        '<text x="585" y="282" text-anchor="middle" font-size="10" fill="#993C1D">EC · CITE · VA on items</text>'
    ),
    "decision": (
        '<rect x="445" y="340" width="215" height="44" rx="6" fill="#E6F1FB" stroke="#185FA5" stroke-width="0.5"/>'
        '<text x="552" y="358" text-anchor="middle" font-size="12" font-weight="500" fill="#0C447C">decision router</text>'
        '<text x="552" y="374" text-anchor="middle" font-size="10" fill="#185FA5">accept · partial_accept · sme_flag</text>'
    ),
}

# Phase aliases — keep the active-phase set forgiving for cross-phase records.
# `dedup` and `supersession` belong to the linker pass; `resolution` is the legacy
# name for vmaw.
_PHASE_ALIAS = {
    "dedup": "linking",
    "supersession": "linking",
    "resolution": "vmaw",
}

# Per-edge arrows. Key is (from_phase, to_phase). The triage→decision "done"
# arrow only renders when triage AND decision fired but NEITHER repair nor vmaw
# did — i.e. the field went straight through with no defects (handled specially).
_EDGE_SVG: dict[tuple[str, str], str] = {
    ("preprocess", "planning"):
        '<line x1="110" y1="62" x2="130" y2="62" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar-n)"/>',
    ("planning", "extraction"):
        '<line x1="210" y1="62" x2="230" y2="62" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar-n)"/>',
    ("extraction", "linking"):
        '<line x1="370" y1="62" x2="390" y2="62" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar-n)"/>',
    ("linking", "verification"):
        '<line x1="490" y1="62" x2="510" y2="62" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar-n)"/>',
    ("verification", "triage"):
        '<line x1="585" y1="84" x2="585" y2="155" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar-n)"/>',
    ("triage", "repair"): (
        '<path d="M 510 177 Q 460 177 460 230 Q 460 270 410 270 L 380 270" '
        'fill="none" stroke="#BA7517" stroke-width="1.5" stroke-dasharray="5 3" marker-end="url(#ar-a)"/>'
        '<text x="320" y="265" fill="#854F0B" font-size="11" font-weight="500">repair loop</text>'
    ),
    ("repair", "linking"): (
        '<path d="M 240 270 Q 200 270 200 200 Q 200 130 360 130 L 440 130 L 440 86" '
        'fill="none" stroke="#BA7517" stroke-width="1.5" stroke-dasharray="5 3" marker-end="url(#ar-a)"/>'
        '<text x="200" y="135" fill="#854F0B" font-size="10">re-assemble · re-verify</text>'
    ),
    ("triage", "vmaw"): (
        '<line x1="585" y1="199" x2="585" y2="248" stroke="#D85A30" stroke-width="1.5" marker-end="url(#ar-c)"/>'
        '<text x="595" y="225" fill="#993C1D" font-size="11">escalate</text>'
    ),
    ("vmaw", "decision"):
        '<line x1="585" y1="292" x2="585" y2="340" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar-n)"/>',
    ("triage", "decision"): (   # "done — no defects" path, conditional
        '<path d="M 540 177 Q 410 177 410 340" fill="none" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar-n)"/>'
        '<text x="405" y="220" fill="#5F5E5A" font-size="11" text-anchor="end">done (no defects)</text>'
    ),
}

_TERMINATION_LEGEND = (
    '<rect x="10" y="350" width="240" height="56" rx="6" fill="#F1EFE8"/>'
    '<text x="22" y="370" font-size="11" font-weight="500" fill="#2C2C2A">Termination guarantees</text>'
    '<text x="22" y="385" font-size="11" fill="#5F5E5A">· per-team repair cap (default 1)</text>'
    '<text x="22" y="398" font-size="11" fill="#5F5E5A">· global budget = n_teams × factor · recur-guard</text>'
)


def pipeline_flow_svg(active_phases: set[str] | list[str] | None = None) -> str:
    """Return the inline SVG string for the pipeline flowchart.

    `active_phases` — if set, every node is STILL rendered (so the SME sees the
    full pipeline architecture), but the phases NOT in the set are dimmed to
    25 % opacity, and arrows whose source or destination is dimmed are dimmed
    too. The phases that DID fire for the selected field stay at full color —
    so at a glance the SME can trace the field's actual path through the same
    diagram (e.g. VMAW is greyed out when VMAW wasn't invoked for that field,
    but it's still on the page so the architecture remains legible).

    When `active_phases` is None (default — doc-level orientation views like the
    Production / Entity browser), every node renders at full opacity."""
    if active_phases is None:
        active = set(_NODE_SVG.keys())
    else:
        active = {_PHASE_ALIAS.get(p, p) for p in active_phases if p}
        # Empty set → treat as doc-level view (full color) instead of an
        # entirely-greyed chart that's hard to read.
        if not active:
            active = set(_NODE_SVG.keys())

    def _wrap_node(svg: str, is_active: bool) -> str:
        # Group the node's rect + labels so we can dim the whole thing in one
        # place. opacity:0.25 keeps it legible-but-clearly-inactive.
        op = "1" if is_active else "0.25"
        return f'<g opacity="{op}">{svg}</g>'

    def _wrap_edge(svg: str, is_active: bool) -> str:
        op = "1" if is_active else "0.2"
        return f'<g opacity="{op}">{svg}</g>'

    parts: list[str] = [
        '<svg viewBox="0 0 680 420" xmlns="http://www.w3.org/2000/svg" role="img"',
        '     aria-label="Pipeline flowchart with the triage repair loopback and VMAW escalation branch"',
        '     style="width:100%;height:auto;font-family:var(--font-sans);">',
        _SVG_DEFS,
    ]
    for phase, node_svg in _NODE_SVG.items():
        parts.append(_wrap_node(node_svg, phase in active))
    for (a, b), edge_svg in _EDGE_SVG.items():
        is_active = (a in active) and (b in active)
        parts.append(_wrap_edge(edge_svg, is_active))
    parts.append(_TERMINATION_LEGEND)
    parts.append('</svg>')
    return "\n".join(parts)
