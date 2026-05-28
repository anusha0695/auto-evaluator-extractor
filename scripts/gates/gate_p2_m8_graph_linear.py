"""
Phase 2 M8 verification gate — graph_linear wiring (deterministic; no cloud/LLM).

Done-when (deterministic portion of PHASE_2_PLAN.md M8):
  - graph_linear imports/compiles;
  - the pure post-team helpers (assemble_and_link + run_verifier_suite) run on
    synthetic section outputs and produce the envelope, links, scorecards, and a
    binding summary;
  - build_linear_graph wires the expected node chain.

The full end-to-end run on demo.pdf (real Gemini/DocAI) is the CHECKPOINT:
  make run-local PDF=./data/actual_docs/demo.pdf   (with --version v2)

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m8_graph_linear.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace as NS

import yaml

from agents.link_binding_verifier import LinkBindingVerifier
from agents.linker import Linker
from agents.planner import Planner
from core.schema_loader import SchemaLoader
from decision.decision_router import DecisionRouter
from pipeline.graph_linear import (
    GraphDependencies,
    assemble_and_link,
    build_linear_graph,
    run_verifier_suite,
)


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")

    print("[1] assemble_and_link (pure)")
    # v3 merge: JAK2 variant is a biomarker with variant_detail; it still links
    # variant_on_panel to the JAK2 panel entry.
    sections = {
        "tested_biomarker_umbrella": {
            "count_of_tested_biomarkers": 2, "page_numbers": [1], "llm_confidence_score": 0.9,
            "tested_biomarkers": ["JAK2", "BRAF"]},
        "other_molecular_biomarker_umbrella": {
            "count": 2, "llm_confidence_score": 0.9,
            "other_molecular_biomarkers": [
                {"biomarker_name": "JAK2", "findings": [
                    {"method": "PCR", "result": "Detected",
                     "occurrences": [{"block_id": "b1", "surface": "JAK2"}],
                     "variant_detail": {"amino_acid_change": "p.V617F"}}]},
                {"biomarker_name": "HER2", "findings": [
                    {"method": "IHC", "result": "2+",
                     "occurrences": [{"block_id": "b2", "surface": "HER2 IHC 2+"}]}]}]},
    }
    blocks = [{"block_id": "b1", "text": "JAK2 V617F Detected"},
              {"block_id": "b2", "text": "HER2 IHC 2+"}]  # narrative (no table_id)
    linker = Linker()
    envelope, links, nrev = assemble_and_link(
        linker=linker, section_outputs=sections, blocks=blocks, block_profiles=[])
    # Generic linker (post-refactor): envelope contains exactly the sections the active
    # teams produced. Only `tested` + `biomarker` were passed in here, so those are the
    # only umbrellas. (Add a section to `sections` above to add it to the envelope.)
    check("envelope emits exactly the produced sections + count",
          set(envelope) == {"count_of_extracted_objects",
                             "other_molecular_biomarker_umbrella", "tested_biomarker_umbrella"},
          str(sorted(envelope)))
    check("JAK2 variant_on_panel link present",
          any(getattr(l, "type", None) == "variant_on_panel" for l in links), str(len(links)))

    print("[2] run_verifier_suite (pure)")
    parser_hyp = {"candidates": [{"text": "JAK2", "target_umbrella": "other_molecular_biomarker_umbrella"}]}
    scorecards, binding = run_verifier_suite(
        schema_loader=sl, binding_verifier=LinkBindingVerifier(),
        envelope=envelope, links=links, blocks=blocks, parser_hypothesis=parser_hyp)
    names = {s["verifier_name"] for s in scorecards}
    check("deterministic verifiers ran (incl. recall_floor + attribution + normalization + hgvs_validity)",
          names == {"schema_validator", "coverage_audit", "link_consistency",
                    "evidence_confidence", "recall_floor", "attribution", "normalization",
                    "hgvs_validity"},
          str(sorted(names)))
    check("binding summary has refuted+uncertain ints",
          set(binding) == {"refuted", "uncertain"} and all(isinstance(v, int) for v in binding.values()),
          str(binding))
    check("narrative HER2 bind → uncertain (escalate)", binding["uncertain"] >= 1, str(binding))

    print("[3] build_linear_graph wires the node chain")
    teams_cfg = yaml.safe_load(open("config/teams.yaml", encoding="utf-8"))
    stub_deps = GraphDependencies(
        storage_config=None, schema_loader=sl, prompt_renderer=None,
        persistence=NS(), preprocess_deps=NS(), planner=Planner(), teams={},
        linker=linker, binding_verifier=LinkBindingVerifier(), decision_router=DecisionRouter(),
    )
    try:
        graph = build_linear_graph(stub_deps, teams_cfg=teams_cfg, checkpointer=None)
        node_names = set(graph.get_graph().nodes)
        expected = {"document_received", "preprocess", "planner", "teams",
                    "linker", "verifiers", "decision_router", "persist"}
        check("all expected nodes wired", expected <= node_names, str(sorted(node_names)))
    except Exception as exc:  # noqa: BLE001
        check("build_linear_graph compiles", False, str(exc))

    print("-" * 60)
    if fails:
        print(f"M8 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M8 VERIFY: PASS — graph_linear helpers + node wiring (end-to-end run = checkpoint).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
