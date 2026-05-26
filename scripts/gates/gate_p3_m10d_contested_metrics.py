"""
Phase 3 M10d gate — per-type contested-rate metrics (pipeline/link_metrics.py) +
persist_v3 emitting the link_metrics artifact. Pure, offline.

Checks:
  [1] links: emitted count per type; a link is 'contested' when a refuted/uncertain
      binding verdict touches an endpoint; contested_rate computed.
  [2] attribution: per-target tally lifted from the AttributionVerifier scorecard,
      with contested_rate = contested / checked.
  [3] escalations_by_kind tallies the SME queue.
  [4] persist_v3 writes the link_metrics artifact on a committed run.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m10d_contested_metrics.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from pipeline.link_metrics import build_link_metrics


_OMB0 = "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]"
_OMB1 = "other_molecular_biomarker_umbrella.other_molecular_biomarkers[1]"
_SP0 = "significant_findings.specimen_findings[0]"
_TB0 = "tested_biomarker_umbrella.tested_biomarkers[0]"


def _state():
    return {
        "links": [
            {"type": "biomarker_on_specimen", "from_ref": _OMB0, "to_ref": _SP0},
            {"type": "biomarker_on_specimen", "from_ref": _OMB1, "to_ref": _SP0},
            {"type": "tested_to_result", "from_ref": _TB0, "to_ref": _OMB1},
        ],
        # a refuted binding touching biomarker[0] → contests one biomarker_on_specimen
        "binding_items": [{"ref": _OMB0, "check": "V2", "verdict": "refuted", "evidence": "x"}],
        "verifier_scorecards": [{"verifier_name": "attribution", "passed": True, "metrics": {
            "specimen_attributes": {"checked": 4, "grounded": 2, "contested": 1,
                                    "ungrounded": 1, "unverified": 0},
            "biomarker_finding_attributes": {"checked": 2, "grounded": 2, "contested": 0,
                                             "ungrounded": 0, "unverified": 0},
        }}],
        "escalation_queue": [{"kind": "attribution_contested"}, {"kind": "binding_refuted"},
                             {"kind": "attribution_contested"}],
    }


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    m = build_link_metrics(_state())

    print("[1] link metrics: emitted + contested + rate")
    lk = m["links"]
    check("biomarker_on_specimen emitted == 2", lk["biomarker_on_specimen"]["emitted"] == 2, str(lk))
    check("biomarker_on_specimen contested == 1 (binding refuted touched [0])",
          lk["biomarker_on_specimen"]["contested"] == 1)
    check("biomarker_on_specimen contested_rate == 0.5", lk["biomarker_on_specimen"]["contested_rate"] == 0.5)
    check("tested_to_result contested == 0 (no binding overlap)", lk["tested_to_result"]["contested"] == 0)

    print("[2] attribution metrics + contested_rate from the scorecard")
    at = m["attribution"]
    check("specimen_attributes checked==4", at["specimen_attributes"]["checked"] == 4)
    check("specimen_attributes contested_rate == 0.25", at["specimen_attributes"]["contested_rate"] == 0.25)
    check("biomarker_finding_attributes contested_rate == 0.0",
          at["biomarker_finding_attributes"]["contested_rate"] == 0.0)

    print("[3] escalations_by_kind")
    esc = m["escalations_by_kind"]
    check("attribution_contested == 2", esc.get("attribution_contested") == 2, str(esc))
    check("binding_refuted == 1", esc.get("binding_refuted") == 1)

    print("[4] persist_v3 writes the link_metrics artifact")
    from pipeline.graph_selfcorrecting import _make_selfcorrecting_persist_node

    class _FakePersistence:
        def __init__(self):
            self.artifacts: dict[str, str] = {}

        async def write_run(self, state):
            return "r"

        async def write_extraction(self, state, *, run_id=None):
            return "e"

        async def write_artifact(self, *, doc_id, kind, content):
            self.artifacts[kind] = content

    st = _state()
    st.update({"doc_id": "m10d", "verdict": "auto_accept", "extraction": {"x": 1},
               "repair_log": [], "block_reads": {}, "vmaw_log": [], "agent_trace": []})
    fp = _FakePersistence()
    asyncio.run(_make_selfcorrecting_persist_node(persistence=fp)(st))
    check("link_metrics artifact emitted by persist node", "link_metrics" in fp.artifacts)
    if "link_metrics" in fp.artifacts:
        parsed = json.loads(fp.artifacts["link_metrics"])
        check("artifact has links + attribution + escalations_by_kind",
              {"links", "attribution", "escalations_by_kind"} <= set(parsed))

    print("-" * 60)
    if fails:
        print(f"P3-M10d VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M10d VERIFY: PASS — per-type contested-rate metrics + persist emit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
