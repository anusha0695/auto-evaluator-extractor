"""
Gate — LLM adjudicators (wiring + success-path parsing + degrade-to-escalate).

No network: the internal Gemini call is monkeypatched to (a) simulate a parsed
result and (b) simulate failure, so we verify the callables map results to the
Linker/Verifier hook contracts and degrade safely.

Run:  PYTHONPATH=. python scripts/gates/gate_p2_adjudicators.py
"""

from __future__ import annotations

import sys

import agents.adjudicators as A
from agents.adjudicators import (
    _MergeOut,
    _SupersessionOut,
    _VerdictOut,
    _LinkOut,
    _LinksOut,
    build_llm_adjudicators,
    llm_adjudicators_enabled,
)


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] build returns the 5 hook callables")
    adj = build_llm_adjudicators()
    expected = {"relationship_confirm", "link_confirm", "link_adjudicator",
                "supersession_resolver", "merge_adjudicator"}
    check("5 callables with correct keys", set(adj) == expected and all(callable(v) for v in adj.values()))

    print("[2] degrade path: LLM unavailable → escalate (no crash)")
    A._Adjudicator.call = lambda self, prompt, model: None   # simulate failure
    adj = build_llm_adjudicators()
    check("relationship_confirm → uncertain",
          adj["relationship_confirm"](name="HER2", finding={"method": "IHC", "result": "2+", "occurrences": []},
                                      blocks=[{"block_id": "b1", "text": "HER2 IHC 2+"}])["verdict"] == "uncertain")
    check("link_confirm → uncertain",
          adj["link_confirm"](link={"from_ref": "a", "to_ref": "b", "type": "x"}, envelope={}, blocks=[])["verdict"] == "uncertain")
    check("link_adjudicator → []",
          adj["link_adjudicator"](envelope={"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [{"biomarker_name": "JAK2"}]}}, blocks=[]) == [])
    check("supersession_resolver → applied False",
          adj["supersession_resolver"](biomarker={"biomarker_name": "HER2", "findings": []},
                                       addendum_text="amended", addendum_block_id="bX")["applied"] is False)
    check("merge_adjudicator → KEEP_SEPARATE",
          adj["merge_adjudicator"](mention_a={}, mention_b={})["decision"] == "KEEP_SEPARATE")

    print("[3] success path: parsed result maps to hook contract")
    A._Adjudicator.call = lambda self, prompt, model: _VerdictOut(verdict="confirmed", evidence="span says so")
    adj = build_llm_adjudicators()
    out = adj["relationship_confirm"](name="HER2", finding={"method": "IHC", "result": "2+", "occurrences": []}, blocks=[])
    check("relationship_confirm → confirmed + evidence", out["verdict"] == "confirmed" and out["evidence"] == "span says so")

    A._Adjudicator.call = lambda self, prompt, model: _LinksOut(
        links=[_LinkOut(from_ref="Genomic_Variant_umbrella.Genomic_Variants[0]",
                        to_ref="x.y[0]", type="finding_interpretation", rationale="same para")])
    adj = build_llm_adjudicators()
    links = adj["link_adjudicator"](envelope={"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [{"biomarker_name": "JAK2"}]}}, blocks=[])
    check("link_adjudicator → 1 contextual link dict", len(links) == 1 and links[0]["type"] == "finding_interpretation")

    A._Adjudicator.call = lambda self, prompt, model: _SupersessionOut(applied=True, method="IHC", amended_result="Negative")
    adj = build_llm_adjudicators()
    bm = {"biomarker_name": "HER2", "findings": [{"method": "IHC", "result": "2+",
          "occurrences": [{"block_id": "b1", "surface": "2+", "superseded": False}]}]}
    res = adj["supersession_resolver"](biomarker=bm, addendum_text="amended to Negative", addendum_block_id="bADD")
    check("supersession applied → result amended + old superseded",
          res["applied"] and bm["findings"][0]["result"] == "Negative"
          and bm["findings"][0]["occurrences"][0]["superseded"] is True
          and any(o.get("block_id") == "bADD" for o in bm["findings"][0]["occurrences"]))

    print("[4] env gate")
    import os
    os.environ.pop("LLM_ADJUDICATORS", None)
    check("enabled by default", llm_adjudicators_enabled() is True)
    os.environ["LLM_ADJUDICATORS"] = "0"
    check("disabled via env", llm_adjudicators_enabled() is False)
    os.environ.pop("LLM_ADJUDICATORS", None)

    print("[5] graph_linear wires them")
    from pathlib import Path
    src = Path("pipeline/graph_linear.py").read_text(encoding="utf-8")
    check("graph_linear builds adjudicators when none injected", "build_llm_adjudicators" in src and "llm_adjudicators_enabled" in src)

    print("-" * 60)
    if fails:
        print(f"ADJUDICATORS VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("ADJUDICATORS VERIFY: PASS — 5 hooks, success-path parse, degrade-to-escalate, wired.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
