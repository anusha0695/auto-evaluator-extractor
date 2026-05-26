"""
Phase 2 M7 verification gate — DecisionRouter.decide_v2 aggregation.

Done-when (PHASE_2_PLAN.md M7): aggregate all teams' verdicts + scorecards +
binding verdicts + confidence → auto_accept / fixable / sme_flag. Refuted bind
→ fixable; uncertain → sme_flag; needs_review → sme_flag.

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m7_decision_router.py
"""

from __future__ import annotations

import sys

from decision.decision_router import DecisionRouter


def main() -> int:
    fails: list[str] = []
    r = DecisionRouter(auto_accept_confidence_threshold=0.85)

    def check(label, state, expected):
        got, reason = r.decide_v2(state)
        ok = got == expected
        print(f"  [{'OK  ' if ok else 'FAIL'}] {label}: {got}" + ("" if ok else f" (expected {expected})"))
        if not ok:
            fails.append(label)

    good_teams = {
        "metadata_team": {"verdict": "committed", "llm_confidence_score": 0.95, "needs_review_count": 0},
        "genomic_variant_team": {"verdict": "committed", "llm_confidence_score": 0.92, "needs_review_count": 0},
    }
    passed_cards = [{"verifier_name": "schema_validator", "passed": True}]

    check("all good → auto_accept",
          {"team_results": good_teams, "verifier_scorecards": passed_cards,
           "binding_verifier": {"refuted": 0, "uncertain": 0}}, "auto_accept")

    check("team sme_flag → sme_flag",
          {"team_results": {**good_teams, "x": {"verdict": "sme_flag"}},
           "verifier_scorecards": passed_cards}, "sme_flag")

    check("failed verifier → sme_flag",
          {"team_results": good_teams,
           "verifier_scorecards": [{"verifier_name": "link_consistency", "passed": False, "notes": "dangling"}]},
          "sme_flag")

    check("binding refuted → fixable",
          {"team_results": good_teams, "verifier_scorecards": passed_cards,
           "binding_verifier": {"refuted": 1, "uncertain": 0}}, "fixable")

    check("binding uncertain → sme_flag",
          {"team_results": good_teams, "verifier_scorecards": passed_cards,
           "binding_verifier": {"refuted": 0, "uncertain": 2}}, "sme_flag")

    check("needs_review → sme_flag",
          {"team_results": {"t": {"verdict": "committed", "llm_confidence_score": 0.99, "needs_review_count": 1}},
           "verifier_scorecards": passed_cards, "binding_verifier": {"refuted": 0, "uncertain": 0}},
          "sme_flag")

    check("low confidence → sme_flag",
          {"team_results": {"t": {"verdict": "committed", "llm_confidence_score": 0.50, "needs_review_count": 0}},
           "verifier_scorecards": passed_cards, "binding_verifier": {"refuted": 0, "uncertain": 0}},
          "sme_flag")

    print("-" * 60)
    if fails:
        print(f"M7 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M7 VERIFY: PASS — decide_v2 aggregates teams + verifiers + binds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
