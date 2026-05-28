"""
V4-M16 gate — field-trace fixes: phase preservation + JSON-only thoughts replaced
+ repair/vmaw deduped + flowchart rendered via components.html.

Locks (offline, deterministic — pure-function asserts on assemble_field_trace):

  [1] assemble_field_trace preserves each record's own `phase` (planning, linking,
      dedup, supersession, verification, triage, repair, vmaw, decision) — no
      hardcoded "extraction" override.
  [2] JSON-only thought content is REPLACED with a structural one-line summary
      ("Model proposed output: N populated field(s).") — never surfaced as the
      field's "why".
  [3] Cross-section agents without a `section` (Planner, DecisionRouter) still
      land in the per-field timeline; agents whose `refs` touch the focused ref
      land regardless of section.
  [4] When agent_trace has phase=repair records, the legacy repair_log channel is
      NOT also emitted (no duplicate rows). Same for vmaw_log.
  [5] Production browser + entity browser + evidence.render_trace all render the
      flowchart via streamlit.components.v1.html (so SVG <defs>/<marker> survive).

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m16_field_trace_phase_and_json.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from ui.phase1.field_trace import _clean_json_reasoning, _looks_like_json, assemble_field_trace


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] phase preserved per record")
    section = "other_molecular_biomarker_umbrella"
    ref = f"{section}.other_molecular_biomarkers[0]"
    trace = [
        {"phase": "planning", "agent": "Planner", "step": 0,
         "refs": [], "section": None, "verdict": "planned",
         "input_summary": "x", "output_summary": "x"},
        {"phase": "extraction", "agent": "Extractor", "step": 1,
         "section": section, "verdict": "extracted",
         "input_summary": "x", "output_summary": "produced 5 field(s)"},
        {"phase": "linking", "agent": "Linker · variant_on_panel", "step": 2,
         "section": None, "refs": [ref, "tested_biomarker_umbrella.tested_biomarkers[0]"],
         "verdict": "deterministic", "reasoning": "Same HGNC gene 'JAK2'.",
         "input_summary": "x", "output_summary": "x"},
        {"phase": "dedup", "agent": "Linker · Dedup", "step": 3,
         "section": section, "refs": [ref], "verdict": "dropped",
         "reasoning": "owning section is Genomic_Variant_umbrella",
         "input_summary": "x", "output_summary": "x"},
        {"phase": "triage", "agent": "Triage · repair · renormalize_field", "step": 4,
         "section": section, "refs": [ref], "verdict": "repair",
         "input_summary": "x", "output_summary": "x"},
        {"phase": "repair", "agent": "RepairExecutor · renormalize_field", "step": 5,
         "section": section, "refs": [ref], "verdict": "applied",
         "input_summary": "x", "output_summary": "x"},
        {"phase": "decision", "agent": "DecisionRouter", "step": 6,
         "section": None, "refs": [], "verdict": "accept",
         "input_summary": "x", "output_summary": "x"},
    ]
    steps = assemble_field_trace(ref=ref, section=section, agent_trace=trace)
    phases_by_agent = {s["node"]: s["phase"] for s in steps}
    check("Planner phase = planning", phases_by_agent.get("Planner") == "planning",
          str(phases_by_agent.get("Planner")))
    check("Extractor phase = extraction", phases_by_agent.get("Extractor") == "extraction")
    check("Linker · variant_on_panel phase = linking",
          phases_by_agent.get("Linker · variant_on_panel") == "linking")
    check("Linker · Dedup phase = dedup", phases_by_agent.get("Linker · Dedup") == "dedup")
    check("Triage phase = triage",
          phases_by_agent.get("Triage · repair · renormalize_field") == "triage")
    check("RepairExecutor phase = repair",
          phases_by_agent.get("RepairExecutor · renormalize_field") == "repair")
    check("DecisionRouter phase = decision",
          phases_by_agent.get("DecisionRouter") == "decision")

    print("[2] JSON-only thoughts replaced with structural summary")
    raw_json = '```json\n{ "count_of_other_molecular_biomarkers": 0, "llm_confidence_score": 1.0, "other_molecular_biomarkers": [] }\n```'
    check("_looks_like_json detects fenced JSON", _looks_like_json(raw_json))
    check("_looks_like_json detects raw object", _looks_like_json('{"x": 1}'))
    check("_looks_like_json returns False on prose",
          not _looks_like_json("The model paused to think about the next step."))
    why, scope = _clean_json_reasoning(raw_json, "field",
        {"agent": "Extractor · thought #1", "output_summary": "produced 0 field(s)"})
    # JSON-only "thought" = model produced its answer in one step. The reasoning
    # happened internally → point the SME at the per-field rationales (where it
    # actually lives) instead of pretending no reasoning happened.
    check("JSON-only 'thought' → labelled as one-step answer + points to per-field rationales",
          ("one step" in why and "per-field rationales" in why
           and "0 populated field" in why), why[:160])

    # Tool result: JSON should be replaced with output_summary or generic
    why2, _ = _clean_json_reasoning(
        '{"page_number": 2, "text": "long…"}', "field",
        {"agent": "Extractor · pdf_page_loader → result",
         "output_summary": "pdf_page_loader returned page 2 text (block excerpt)"})
    check("tool result JSON → uses the structural output_summary",
          "pdf_page_loader returned page 2 text" in why2, why2[:80])

    print("[3] phase=repair record correctly surfaces in the trace via the agent_trace loop")
    repair_steps = [s for s in steps if s["phase"] == "repair"]
    check("RepairExecutor row appears under phase=repair", len(repair_steps) == 1, str(repair_steps))

    print("[4] repair_log / vmaw_log channels are deduped when agent_trace already has them")
    repair_log = [
        {"cycle": 1, "action": "renormalize_field", "target_ref": ref, "section": section,
         "outcome": "applied"},
    ]
    deduped = assemble_field_trace(ref=ref, section=section,
                                   agent_trace=trace, repair_log=repair_log)
    repair_count = sum(1 for s in deduped if s["phase"] == "repair")
    check("only ONE repair-phase row even with repair_log present", repair_count == 1,
          str(repair_count))
    # legacy path: no agent_trace repair phase → repair_log fires
    legacy_trace = [a for a in trace if a["phase"] != "repair"]
    legacy = assemble_field_trace(ref=ref, section=section,
                                  agent_trace=legacy_trace, repair_log=repair_log)
    legacy_repair = sum(1 for s in legacy if s["phase"] == "repair")
    check("legacy v3 (no phase=repair in trace) still surfaces repair_log row",
          legacy_repair == 1, str(legacy_repair))

    vmaw_log = [{"resolution": {"item_ref": ref, "capability": "CITE", "status": "proposed"}}]
    trace_with_vmaw = trace + [
        {"phase": "vmaw", "agent": "VMAW · CITE", "step": 7, "section": section,
         "refs": [ref], "verdict": "proposed",
         "input_summary": "x", "output_summary": "x"}]
    via_trace = assemble_field_trace(ref=ref, section=section,
                                     agent_trace=trace_with_vmaw, vmaw_log=vmaw_log)
    vmaw_count = sum(1 for s in via_trace if s["phase"] == "vmaw" or s["phase"] == "resolution")
    check("only ONE vmaw/resolution row even with vmaw_log present", vmaw_count == 1,
          str(vmaw_count))

    print("[5] flowchart now rendered via components.html, not sanitised markdown")
    for path in ("ui/phase1/views/production_browser_view.py",
                 "ui/phase1/views/entity_browser_view.py",
                 "ui/phase1/evidence.py"):
        src = Path(path).read_text(encoding="utf-8")
        check(f"{Path(path).name} renders flowchart via components.html",
              "components.html" in src and "pipeline_flow_svg" in src)

    print("[6] cross-section agents without section still land in the timeline")
    p_present = any(s["node"] == "Planner" for s in steps)
    dr_present = any(s["node"] == "DecisionRouter" for s in steps)
    check("Planner record present despite section=None", p_present)
    check("DecisionRouter record present despite section=None", dr_present)

    print("-" * 60)
    if fails:
        print(f"V4-M16 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M16 VERIFY: PASS — phase preserved, JSON thoughts cleaned, repair/vmaw deduped, flowchart rendered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
