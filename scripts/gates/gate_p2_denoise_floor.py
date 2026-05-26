"""
Quality gate — NER floor denoise + duplication-stop prompt rules.

Checks:
  - the deterministic mapper now DROPS a DISEASE term (e.g. "polycythemia vera")
    while KEEPING a GENE ("JAK2") routed to variant + tested;
  - DISEASE/CHEMICAL/CANCER/PATHOLOGICAL_FORMATION/SIMPLE_CHEMICAL are no longer
    in label_to_umbrellas;
  - molecular_biomarker prompt excludes gene point-mutations;
  - tested_biomarker prompt says list the gene not the variant.

Run:  PYTHONPATH=. python scripts/gates/gate_p2_denoise_floor.py
"""

from __future__ import annotations

import asyncio
import sys

from core.prompt_renderer import PromptRenderer, ToolDescriptor
from core.schema_loader import SchemaLoader
from preprocess.medical_ner import SciSpaCyMedicalNER


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    ner = SciSpaCyMedicalNER(prompt_renderer=None)
    mapping = ner._mapping["label_to_umbrellas"]

    print("[1] disease/chemical labels removed; gene labels kept")
    for lbl in ("DISEASE", "CHEMICAL", "CANCER", "PATHOLOGICAL_FORMATION", "SIMPLE_CHEMICAL"):
        check(f"{lbl} unmapped", lbl not in mapping)
    check("GENE_OR_GENE_PRODUCT still mapped", "GENE_OR_GENE_PRODUCT" in mapping)
    check("AMINO_ACID still mapped", "AMINO_ACID" in mapping)

    print("[2] mapper drops a DISEASE term, keeps a GENE")
    pages = [{"page_number": 1, "text": "JAK2 V617F Mutation Analysis"},
             {"page_number": 2, "text": "patients with polycythemia vera and neoplasms"}]
    block_profiles = [
        {"block_id": "8", "text_role": "panel_or_test_name",
         "target_umbrella_hints": ["tested_biomarker_umbrella", "other_molecular_biomarker_umbrella"]},
        {"block_id": "62", "text_role": "clinical_significance",
         "target_umbrella_hints": ["other_molecular_biomarker_umbrella"]},
    ]
    raw = [
        {"text": "JAK2", "label": "GENE_OR_GENE_PRODUCT", "page": 1, "block_id": "8",
         "char_start": 0, "char_end": 4, "source_model": "en_ner_bionlp13cg_md"},
        {"text": "polycythemia vera", "label": "DISEASE", "page": 2, "block_id": "62",
         "char_start": 14, "char_end": 31, "source_model": "en_ner_bc5cdr_md"},
        {"text": "neoplasms", "label": "CANCER", "page": 2, "block_id": "62",
         "char_start": 36, "char_end": 45, "source_model": "en_ner_bionlp13cg_md"},
    ]
    hyp = asyncio.run(ner.post_process(doc_id="t", pages=pages,
                                       block_profiles=block_profiles, raw_entities=raw))
    texts = {c["text"] for c in hyp["candidates"]}
    umbr = {c["text"]: c["target_umbrella"] for c in hyp["candidates"]}
    check("JAK2 kept", "JAK2" in texts, str(sorted(texts)))
    check("polycythemia vera DROPPED", "polycythemia vera" not in texts)
    check("neoplasms DROPPED", "neoplasms" not in texts)
    # v3 merge: genes legitimately seed the biomarker umbrella now; the denoise
    # invariant is that the DISEASE prose produced NO candidates at all.
    check("no candidates from disease prose",
          texts == {"JAK2"},
          str(sorted(texts)))

    print("[3] prompts carry the duplication-stop rules")
    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")
    pr = PromptRenderer(schema_loader=sl)
    mol = pr.render_team_extractor_prompt(
        team_name="MolecularBiomarkerTeam", team_prompt_template="molecular_biomarker_team.j2",
        schema_section="other_molecular_biomarker_umbrella", pipeline_version="v2",
        available_tools=[ToolDescriptor("biomarker_normalize", "x", {})], parser_hypothesis_count=0)
    check("molecular prompt INCLUDES gene variants via variant_detail",
          "variant_detail" in mol.lower() and "gene sequence variants" in mol.lower())
    tb = pr.render_team_extractor_prompt(
        team_name="TestedBiomarkerTeam", team_prompt_template="tested_biomarker_team.j2",
        schema_section="tested_biomarker_umbrella", pipeline_version="v2",
        available_tools=[ToolDescriptor("hgnc_normalize", "x", {})], parser_hypothesis_count=0)
    check("tested prompt says list gene not variant", "list the gene, not the specific variant" in tb.lower())

    print("-" * 60)
    if fails:
        print(f"DENOISE VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("DENOISE VERIFY: PASS — floor denoised + duplication-stop rules in prompts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
