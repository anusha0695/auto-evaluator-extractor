"""
Phase 3 M2 gate — Option B per-team block index in the extractor user message.

Checks the Extractor's first message now carries a compact index of the blocks
the Block Profiler routed to THIS team's section (block_id/page/role/bbox/snippet),
that non-routed blocks are excluded, that the full page text still follows, and
that it degrades gracefully when no block_profiles exist.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m2_extractor_block_index.py
"""

from __future__ import annotations

import sys

from agents.extractor import Extractor
from core.prompt_renderer import PromptRenderer
from core.schema_loader import SchemaLoader


def _extractor(section: str) -> Extractor:
    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")
    pr = PromptRenderer(schema_loader=sl)
    return Extractor(
        team_name="MolecularBiomarkerTeam", schema_section=section,
        team_prompt_template="molecular_biomarker_team.j2",
        tool_allowlist=["pdf_page_loader"], prompt_renderer=pr, schema_loader=sl,
        pipeline_version="v2", model_name="gemini-2.5-pro", temperature=0.0,
    )


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    ex = _extractor("other_molecular_biomarker_umbrella")

    state = {"doc_id": "t", "doc_profile": {
        "total_pages": 1,
        "pages": [{"page_number": 1, "text": "Ancillary studies: ER Positive 95% by IHC. Patient Jane."}],
        "blocks": [
            {"block_id": "b1", "page_number": 1, "bbox": [0.10, 0.20, 0.50, 0.30],
             "text": "ER: Positive (95% of tumor nuclei) by IHC"},
            {"block_id": "b2", "page_number": 1, "bbox": [0.10, 0.40, 0.50, 0.50],
             "text": "Patient Name: Jane Sample"},
        ],
        "block_profiles": [
            {"block_id": "b1", "page_number": 1, "text_role": "results_table",
             "target_umbrella_hints": ["other_molecular_biomarker_umbrella", "tested_biomarker_umbrella"]},
            {"block_id": "b2", "page_number": 1, "text_role": "patient_demographics",
             "target_umbrella_hints": ["report_metadata"]},
        ],
    }}

    print("[1] block index present, routed-only, page text retained")
    msg = ex._build_user_message(state, None)
    check("index header present", "Blocks the profiler routed to" in msg)
    check("routed block b1 indexed (role results_table)", "b1" in msg and "results_table" in msg)
    check("bbox rendered for b1", "[0.10,0.20,0.50,0.30]" in msg)
    check("snippet rendered for b1", "ER: Positive" in msg)
    check("non-routed block b2 (report_metadata) excluded from index",
          "patient_demographics" not in msg)
    check("full page text still present", "Ancillary studies" in msg)

    print("[2] metadata team sees b2, not b1")
    ex_md = _extractor("report_metadata")
    msg_md = ex_md._build_user_message(state, None)
    check("metadata index has patient_demographics", "patient_demographics" in msg_md)
    check("metadata index excludes results_table", "results_table" not in msg_md)

    print("[3] graceful when no block_profiles")
    msg_empty = ex._build_user_message({"doc_id": "t", "doc_profile": {
        "total_pages": 1, "pages": [{"page_number": 1, "text": "hello"}]}}, None)
    check("no index section when profiles absent", "Blocks the profiler routed to" not in msg_empty)
    check("page text still present", "hello" in msg_empty)

    print("-" * 60)
    if fails:
        print(f"P3-M2 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M2 VERIFY: PASS — per-team block index injected (routed-only, graceful).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
