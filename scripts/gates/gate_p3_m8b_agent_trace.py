"""
Phase 3 M8b gate — per-agent trace assembly (pipeline/agent_trace.py).

Fully offline — stub result objects stand in for live SectionTeamResults. Checks:
  [1] happy path (extractor + auditor commit) → 2 ordered records, no arbiter.
  [2] conflict path (extractor + auditor gap + arbiter RE_EXTRACT + retry) → 4
      ordered records with the right agents/verdicts/hint count.
  [3] PHI discipline: output summaries are structural (field COUNTS, verdicts),
      never the raw extracted values / prompt text.
  [4] aggregate_team_traces flattens multiple teams in order.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m8b_agent_trace.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace as NS

from pipeline.agent_trace import aggregate_team_traces, build_team_trace


def _ex(output, conf=0.9, reasoning="found the value in the results table"):
    return NS(output=output, llm_confidence_score=conf, latency_ms=120, tool_calls_made=2,
              reasoning_trace=[{"role": "user", "content": "extract"},
                               {"role": "assistant", "content": reasoning}])


def _result(section, *, extractor=None, auditor=None, arbiter=None, retry=None):
    return NS(schema_section=section, extractor_result=extractor, auditor_result=auditor,
              arbiter_result=arbiter, extractor_result_retry=retry)


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    secret_value = "STANLEY D SCHINKE, M.D."   # a value that must NOT leak into the trace

    print("[1] happy path → extractor + auditor")
    r1 = _result("report_metadata",
                 extractor=_ex({"Additional_Provider_Name": secret_value, "page": 1}),
                 auditor=NS(coverage_ok=True, gap_signal=False, parser_hypothesis_misses=[], latency_ms=80))
    t1 = build_team_trace(r1, team_key="metadata_team")
    # Post-M13: build_team_trace expands the Extractor's reasoning_trace into per-
    # Thought/Tool records, so the count is now (high-level agents + reasoning steps).
    # The high-level agents are unchanged; verify them explicitly.
    hi_t1 = [r["agent"] for r in t1 if not r["agent"].startswith("Extractor · ")]
    check("steps are strictly increasing", [r["step"] for r in t1] == list(range(len(t1))))
    check("high-level agents = [Extractor, CoverageAuditor]",
          hi_t1 == ["Extractor", "CoverageAuditor"], str(hi_t1))
    check("reasoning-chain records appear (Thought / Tool)",
          any(r["agent"].startswith("Extractor · ") for r in t1))
    check("auditor verdict coverage_ok",
          next(r for r in t1 if r["agent"] == "CoverageAuditor")["verdict"] == "coverage_ok")
    check("extractor counted 2 populated fields",
          "2 populated field(s)" in next(r for r in t1 if r["agent"] == "Extractor")["output_summary"])

    print("[2] conflict path → extractor + auditor(gap) + arbiter + retry")
    r2 = _result("other_molecular_biomarker_umbrella",
                 extractor=_ex({"other_molecular_biomarkers": [{"biomarker_name": "JAK2"}]}, conf=0.6),
                 auditor=NS(coverage_ok=False, gap_signal=True, parser_hypothesis_misses=["HER2"], latency_ms=90,
                            auditor_notes="HER2 appears in the ancillary studies table but is absent from the output"),
                 arbiter=NS(policy="RE_EXTRACT", reasoning="missed HER2 in ancillary table",
                            re_extract_hints=[{"field_name": "HER2", "hint": "look in ancillary studies"}]),
                 retry=_ex({"other_molecular_biomarkers": [{"biomarker_name": "JAK2"}, {"biomarker_name": "HER2"}]}, conf=0.92))
    t2 = build_team_trace(r2, team_key="molecular_biomarker_team")
    hi_t2 = [r["agent"] for r in t2 if not r["agent"].startswith("Extractor · ")]
    check("high-level agents in order",
          hi_t2 == ["Extractor", "CoverageAuditor", "Arbiter", "Extractor (re-extract)"], str(hi_t2))
    check("auditor flagged gap",
          next(r for r in t2 if r["agent"] == "CoverageAuditor")["verdict"] == "gap_flagged")
    arb = next(r for r in t2 if r["agent"] == "Arbiter")
    check("arbiter verdict RE_EXTRACT", arb["verdict"] == "RE_EXTRACT")
    check("arbiter records hint count", "1 hint(s)" in arb["output_summary"], arb["output_summary"])
    rx = next(r for r in t2 if r["agent"] == "Extractor (re-extract)")
    check("retry input notes 1 hint", "1 focused hint" in rx["input_summary"], rx["input_summary"])

    print("[3] PHI discipline — extracted field VALUES are not dumped (only counts)")
    blob = str(t1) + str(t2)
    # the guarantee: the extractor's output dict (raw clinical values) is summarized
    # as a COUNT, never serialized into the trace. (Arbiter reasoning text — the
    # agent's rationale — IS included intentionally; trace artifacts are local-only.)
    check("extracted value (provider name) not dumped", secret_value not in blob)
    check("extractor (high-level) output is a field COUNT, not the raw dict",
          all("populated field(s)" in r["output_summary"]
              for r in (t1 + t2)
              if r["agent"] in ("Extractor", "Extractor (re-extract)")))

    print("[3b] each agent carries its own reasoning")
    by_agent = {r["agent"]: r for r in t2}
    check("Extractor reasoning captured (from ReAct trace)",
          "results table" in by_agent["Extractor"]["reasoning"], by_agent["Extractor"]["reasoning"][:60])
    check("CoverageAuditor reasoning = auditor_notes",
          "ancillary studies table" in by_agent["CoverageAuditor"]["reasoning"])
    check("Arbiter reasoning captured", "missed HER2" in by_agent["Arbiter"]["reasoning"])
    check("re-extract reasoning captured", by_agent["Extractor (re-extract)"]["reasoning"] != "")
    # per-field reasons captured from auditor missed_fields + arbiter hints
    check("Arbiter field_reasons keyed by field (HER2)",
          by_agent["Arbiter"]["field_reasons"].get("HER2", "").startswith("look") or "HER2" in by_agent["Arbiter"]["field_reasons"],
          str(by_agent["Arbiter"]["field_reasons"]))
    # extractor reasoning must NOT be a JSON/code dump
    check("extractor reasoning is not a JSON dump",
          not by_agent["Extractor"]["reasoning"].lstrip().startswith(("{", "[", "```", '"')))

    print("[4] aggregate across teams")
    agg = aggregate_team_traces({"metadata_team": r1, "molecular_biomarker_team": r2})
    # Aggregate now includes high-level agents + their expanded reasoning steps.
    # Verify the high-level skeleton without depending on the reasoning expansion count.
    hi_agents = [r["agent"] for r in agg if not r["agent"].startswith("Extractor · ")]
    check("aggregate carries every high-level team agent",
          hi_agents == ["Extractor", "CoverageAuditor",
                        "Extractor", "CoverageAuditor", "Arbiter", "Extractor (re-extract)"],
          str(hi_agents))
    check("teams tagged", {r["team"] for r in agg} == {"metadata_team", "molecular_biomarker_team"})

    print("-" * 60)
    if fails:
        print(f"P3-M8b VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M8b VERIFY: PASS — per-agent trace assembled, ordered, PHI-safe, aggregated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
