"""
V4-M14 gate — Option C field view + pipeline flowchart wired into the UI.

Locks (offline, deterministic — pure HTML/SVG string output, no Streamlit import):

  [1] `render_field_flow_html(steps)` returns one row per step with phase chip,
      agent name, inline reasoning, and verdict pill — no row is silently dropped.
  [2] Phase chips render the canonical colors (preprocess gray, extraction blue,
      linking purple, dedup purple, verification teal, triage amber, repair amber,
      vmaw coral, decision blue) — adding a new phase is one map entry.
  [3] Repair-cycle bands appear when the trace contains repair steps — one band
      per cycle ("Repair cycle 2", "Repair cycle 3" …) inserted before the cycle's
      first repair row.
  [4] `binding` rows are hidden by default and surface when show_binding=True.
  [5] `pipeline_flow_svg()` returns the loop-back flowchart — preprocess → planner →
      teams → linker → verifiers → triage, with a labelled REPAIR LOOP arc back to
      the linker, an ESCALATE arc to VMAW, and a "done (no defects)" branch to the
      decision router. Termination callout naming the three guarantees is present.
  [6] `evidence.render_trace` delegates to the new renderer (source-inspection).

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m14_field_view.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from ui.phase1.field_view import pipeline_flow_svg, render_field_flow_html


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] render_field_flow_html — one row per step")
    steps = [
        {"phase": "preprocess", "node": "BlockProfiler", "technical": "67 profiles",
         "plain": "67 blocks classified", "verdict": "profiled",
         "reasoning": "results_table tagged on p.1"},
        {"phase": "extraction", "node": "Extractor", "technical": "extracted 24 fields",
         "plain": "pulled 24 values", "verdict": "extracted", "reasoning": "JAK2 V617F in column"},
        {"phase": "linking", "node": "Linker · variant_on_panel",
         "technical": "JAK2 ↔ panel", "plain": "linked variant to panel",
         "verdict": "deterministic", "reasoning": "Same HGNC-normalized gene 'JAK2'."},
        {"phase": "dedup", "node": "Linker · Dedup",
         "technical": "dropped biomarker[0]", "plain": "removed duplicate",
         "verdict": "dropped", "reasoning": "same gene present in variant section"},
        {"phase": "verification", "node": "hgvs_validity",
         "technical": "all valid", "plain": "HGVS structural ok", "verdict": "passed"},
        {"phase": "verification", "node": "LinkBindingVerifier · V3",
         "kind": "binding", "technical": "uncertain", "plain": "binding ambiguous",
         "verdict": "uncertain", "reasoning": "not co-mentioned"},
        {"phase": "decision", "node": "DecisionRouter",
         "technical": "verdict accept", "plain": "accepted", "verdict": "accept",
         "reasoning": "all teams converged"},
    ]
    out = render_field_flow_html(steps, ref="Genomic_Variants[0].result", value="Not Detected")
    # Hidden binding by default — 6 visible.
    check("emits a row per visible step (binding hidden by default)",
          out.count("grid-template-columns:110px 1fr 110px") == 6,
          f"rows={out.count('grid-template-columns:110px 1fr 110px')}")
    check("includes the field header with the ref + value",
          "Genomic_Variants[0].result" in out and "Not Detected" in out)
    check("inline reasoning is shown for steps that carry one",
          "Same HGNC-normalized gene" in out and "JAK2 V617F in column" in out)
    check("verdict pills appear",
          all(v in out for v in ("extracted", "passed", "accept", "dropped")))

    print("[2] phase chips render canonical colors")
    PHASES = {
        "preprocess": "#F1EFE8", "extraction": "#E6F1FB", "linking": "#EEEDFE",
        "dedup": "#EEEDFE", "verification": "#E1F5EE", "triage": "#FAEEDA",
        "repair": "#FAEEDA", "vmaw": "#FAECE7", "decision": "#E6F1FB",
    }
    chip_check = render_field_flow_html(
        [{"phase": p, "node": p, "technical": "x", "plain": "x", "verdict": ""} for p in PHASES])
    for p, color in PHASES.items():
        check(f"phase '{p}' uses {color}", color in chip_check, f"missing {color}")

    print("[3] repair cycle bands appear when repair steps are present")
    with_repair = render_field_flow_html([
        {"phase": "verification", "node": "LinkBindingVerifier", "technical": "uncertain",
         "plain": "x", "verdict": "uncertain"},
        {"phase": "triage", "node": "Triage", "technical": "queued repair", "plain": "x",
         "verdict": "repair"},
        {"phase": "repair", "node": "RepairExecutor", "technical": "re_extract_team",
         "plain": "x", "verdict": "applied"},
        {"phase": "extraction", "node": "Extractor (re-extract)", "technical": "re-extracted",
         "plain": "x", "verdict": "re_extracted"},
        {"phase": "verification", "node": "LinkBindingVerifier", "technical": "passed",
         "plain": "x", "verdict": "passed"},
    ])
    check("a 'Repair cycle N' band is inserted before the repair row",
          "Repair cycle 2" in with_repair, "no cycle band found")
    check("the cycle band is amber-styled",
          "#FAEEDA" in with_repair and "#633806" in with_repair)
    no_repair = render_field_flow_html([
        {"phase": "extraction", "node": "Extractor", "technical": "x", "plain": "x", "verdict": "extracted"}])
    check("no cycle band when there are no repair steps", "Repair cycle" not in no_repair)

    print("[4] binding rows gated by show_binding")
    binding_steps = [
        {"phase": "extraction", "node": "Extractor", "technical": "x", "plain": "x", "verdict": "extracted"},
        {"phase": "verification", "node": "LinkBindingVerifier · V4",
         "kind": "binding", "technical": "uncertain", "plain": "x", "verdict": "uncertain"},
    ]
    hidden = render_field_flow_html(binding_steps)
    shown = render_field_flow_html(binding_steps, show_binding=True)
    check("binding hidden by default", "LinkBindingVerifier · V4" not in hidden)
    check("binding shown when show_binding=True", "LinkBindingVerifier · V4" in shown)

    print("[5] pipeline_flow_svg shows the loop-back + escalate branches")
    svg = pipeline_flow_svg()
    check("svg has all the pipeline nodes",
          all(n in svg for n in ("preprocess", "planner", "teams", "linker",
                                 "verifiers", "triage", "repair", "vmaw",
                                 "decision router")))
    check("svg labels the repair LOOP",
          "repair loop" in svg and "re-assemble" in svg.lower(),
          "loop label not found")
    check("svg labels the escalate branch to VMAW", "escalate" in svg)
    check("svg labels the no-defect direct path to decision",
          "done (no defects)" in svg)
    check("svg names the three termination guarantees",
          all(k in svg for k in ("per-team", "budget", "recur-guard")),
          "termination callout missing")
    # uses amber for loopback, coral for escalate, neutral for forward
    check("svg uses amber for the loopback arrow (#BA7517 marker)",
          "ar-a" in svg and "BA7517" in svg)
    check("svg uses coral for the escalation arrow (#D85A30 marker)",
          "ar-c" in svg and "D85A30" in svg)

    print("[6] evidence.render_trace delegates to the new renderer")
    src = Path("ui/phase1/evidence.py").read_text(encoding="utf-8")
    check("evidence.render_trace imports field_view",
          "from ui.phase1.field_view import" in src)
    check("evidence.render_trace calls render_field_flow_html",
          "render_field_flow_html(" in src)
    check("evidence.render_trace plumbs show_flowchart",
          "pipeline_flow_svg" in src and "show_flowchart" in src)

    print("-" * 60)
    if fails:
        print(f"V4-M14 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M14 VERIFY: PASS — Option C field view + pipeline flowchart wired into ui/phase1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
