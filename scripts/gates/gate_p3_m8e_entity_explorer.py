"""
Phase 3 M8e gate — entity-explorer payload + canvas contract (the Block-explorer-
style Entity browser). Pure, offline.

Checks:
  [1] build_entity_payload: per-entity page + highlight boxes; status review/accepted
      from flagged_refs; trace attached.
  [2] linked entities resolved via links → linked_boxes populated (different layer).
  [3] build_entity_explorer_html embeds the data + the Review/Accepted/Technical/
      Binding controls + the collapsible trace panel.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m8e_entity_explorer.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace as NS

from ui.phase1.components.entity_explorer import build_entity_explorer_html
from ui.phase1.evidence import build_entity_payload, enumerate_entities

_HER2_REF = "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0].findings[0]"
_PANEL_REF = "tested_biomarker_umbrella.tested_biomarkers[0]"


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    # two entities on page 1; HER2 linked to the panel entry via a link
    views = {
        "b1": NS(block_id="b1", page_number=1, bbox=[0.10, 0.20, 0.90, 0.25], text="HER2 IHC equivocal by IHC"),
        "b2": NS(block_id="b2", page_number=1, bbox=[0.10, 0.40, 0.90, 0.45], text="Panel: HER2 tested"),
    }
    entities = [
        {"ref": _HER2_REF, "label": "HER2", "section": "other_molecular_biomarker_umbrella",
         "surface": "HER2", "candidate_block_ids": ["b1"]},
        {"ref": _PANEL_REF, "label": "HER2 (panel)", "section": "tested_biomarker_umbrella",
         "surface": "HER2", "candidate_block_ids": ["b2"]},
    ]
    links = [{"from_ref": _HER2_REF, "to_ref": _PANEL_REF, "type": "variant_on_panel"}]
    agent_trace = [{"step": 0, "agent": "Extractor", "section": "other_molecular_biomarker_umbrella",
                    "output_summary": "produced 1 field", "verdict": "extracted", "confidence": 0.7,
                    "reasoning": "read HER2 from the ancillary row"}]
    flagged = {_HER2_REF}

    print("[0] enumerate_entities returns ALL extracted values, not just results")
    envelope = {
        "report_metadata": {"patient_name": "Jane Sample", "accession_id": "S-24-001",
                            "llm_confidence_score": 0.9, "page_numbers": [1]},
        "tested_biomarker_umbrella": {"count_of_tested_biomarkers": 2,
                                      "tested_biomarkers": ["JAK2", "BRAF"]},
        "other_molecular_biomarker_umbrella": {"count": 1, "other_molecular_biomarkers": [
            {"biomarker_name": "JAK2", "biomarker_class": "sequence_variant", "findings": [
                {"method": "PCR", "result": "Detected",
                 "occurrences": [{"block_id": "b1", "surface": "JAK2 V617F Detected"}]}]}]},
    }
    ents = enumerate_entities(envelope)
    labels = " | ".join(e["surface"] for e in ents)
    check("report metadata field enumerated (patient_name)", any(e["surface"] == "Jane Sample" for e in ents), labels)
    check("biomarker NAME enumerated (not just result)", any(e["surface"] == "JAK2" and "biomarker_name" in e["ref"] for e in ents))
    check("biomarker result enumerated", any(e["surface"] == "Detected" for e in ents))
    check("tested biomarkers enumerated (list of strings)",
          any(e["surface"] == "BRAF" for e in ents))
    check("bookkeeping skipped (no confidence/count/page_numbers)",
          not any(e["surface"] in ("0.9", "2", "1") and ("confidence" in e["ref"] or "count" in e["ref"] or "page_numbers" in e["ref"]) for e in ents))
    check("biomarker_name inherits its finding's block provenance",
          any(e["surface"] == "JAK2" and "biomarker_name" in e["ref"] and e["candidate_block_ids"] == ["b1"] for e in ents))

    print("[1] payload: page + boxes + status + trace")
    payload = build_entity_payload(entities, views=views, word_geometry=[], links=links,
                                   agent_trace=agent_trace, flagged_refs=flagged)
    by_ref = {p["ref"]: p for p in payload}
    her2 = by_ref[_HER2_REF]
    check("HER2 on page 1", her2["page"] == 1, str(her2["page"]))
    check("HER2 has highlight boxes", len(her2["boxes"]) >= 1, str(her2["boxes"]))
    check("HER2 status = review (flagged)", her2["status"] == "review")
    check("panel status = accepted", by_ref[_PANEL_REF]["status"] == "accepted")
    check("HER2 trace attached (extraction step)", any(s["phase"] == "extraction" for s in her2["trace"]))
    check("trace step carries reasoning", any(s.get("reasoning") for s in her2["trace"]))

    print("[2] linked entities resolved")
    check("HER2 linked_boxes populated (panel entry)", len(her2["linked_boxes"]) >= 1, str(her2["linked_boxes"]))
    check("linked box differs from own box", her2["linked_boxes"] != her2["boxes"])

    print("[3] canvas embeds data + controls")
    html = build_entity_explorer_html(payload, pages=[{"page_number": 1, "width": 800, "height": 1000, "data_uri": "data:,"}])
    for token in ("ee-f-review", "ee-f-accepted", "ee-tech", "ee-bind", "ee-trace-head", "ee-divider"):
        check(f"control present: {token}", token in html)
    check("entity data embedded", _HER2_REF in html)
    # light translucent wash (NOT multiply — that goes near-black on dark scans) +
    # a solid outline on the SELECTED entity so it stays visible over overlaps.
    check("no multiply blend (too dark on scans)", "mix-blend-mode:multiply" not in html)
    check("selected entity gets an outline", "entityOutline" in html and "outline:2px solid" in html)

    print("[4] structural within-record linking is GENERIC (not biomarker-only)")
    BSEC = "other_molecular_biomarker_umbrella"
    BREC = f"{BSEC}.other_molecular_biomarkers[0]"
    SSEC = "significant_findings"
    SREC = f"{SSEC}.specimen_findings[0]"
    struct = [
        {"ref": f"{BREC}.biomarker_name", "label": "JAK2", "section": BSEC, "surface": "JAK2", "candidate_block_ids": []},
        {"ref": f"{BREC}.findings[0].result", "label": "result", "section": BSEC, "surface": "Detected", "candidate_block_ids": []},
        {"ref": f"{BREC}.variant_detail.amino_acid_change", "label": "V617F", "section": BSEC, "surface": "V617F", "candidate_block_ids": []},
        {"ref": f"{BSEC}.other_molecular_biomarkers[1].biomarker_name", "label": "BRAF", "section": BSEC, "surface": "BRAF", "candidate_block_ids": []},
        {"ref": f"{SREC}.specimen[0].tissue_type", "label": "tissue", "section": SSEC, "surface": "Breast", "candidate_block_ids": []},
        {"ref": f"{SREC}.pTNM_staging_details.pTNM_stage", "label": "pT2", "section": SSEC, "surface": "pT2", "candidate_block_ids": []},
        {"ref": f"{SSEC}.specimen_findings[1].gross_description.text", "label": "other", "section": SSEC, "surface": "other", "candidate_block_ids": []},
        {"ref": "report_metadata.Patient_First_Name", "label": "name", "section": "report_metadata", "surface": "William", "candidate_block_ids": []},
        {"ref": "report_metadata.Vendor_Name", "label": "vendor", "section": "report_metadata", "surface": "NeoGenomics", "candidate_block_ids": []},
    ]
    sp = {p["ref"]: set(p["linked_refs"]) for p in
          build_entity_payload(struct, views={}, word_geometry=[], links=[], agent_trace=[])}
    check("biomarker_name → its findings", f"{BREC}.findings[0].result" in sp[f"{BREC}.biomarker_name"])
    check("biomarker_name → its variant_detail", f"{BREC}.variant_detail.amino_acid_change" in sp[f"{BREC}.biomarker_name"])
    check("finding result → back to biomarker_name (bidirectional)", f"{BREC}.biomarker_name" in sp[f"{BREC}.findings[0].result"])
    check("biomarker record 0 does NOT link record 1", f"{BSEC}.other_molecular_biomarkers[1].biomarker_name" not in sp[f"{BREC}.biomarker_name"])
    check("significant_findings: tissue_type → staging in same record", f"{SREC}.pTNM_staging_details.pTNM_stage" in sp[f"{SREC}.specimen[0].tissue_type"])
    check("significant_findings record 0 does NOT link record 1", f"{SSEC}.specimen_findings[1].gross_description.text" not in sp[f"{SREC}.specimen[0].tissue_type"])
    check("flat report_metadata field is NOT mass-linked", sp["report_metadata.Patient_First_Name"] == set(), str(sp["report_metadata.Patient_First_Name"]))

    print("[5] provenance reader handles BOTH the new array and the legacy map")
    from ui.phase1.evidence import field_rationale_map
    key = "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0].biomarker_name"
    env_arr = {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
        {"biomarker_name": "JAK2", "provenance": [
            {"field_name": "biomarker_name", "block_id": "b1", "type": "verbatim", "rationale": "stated in header"}]}]}}
    env_map = {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
        {"biomarker_name": "JAK2", "provenance": {
            "biomarker_name": {"block_id": "b1", "type": "verbatim", "rationale": "stated in header"}}}]}}
    check("array provenance → rationale read", field_rationale_map(env_arr).get(key) == "stated in header")
    check("legacy map provenance → rationale read (back-compat)",
          field_rationale_map(env_map).get(key) == "stated in header")

    print("-" * 60)
    if fails:
        print(f"P3-M8e VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M8e VERIFY: PASS — entity payload (boxes/status/links/trace) + canvas contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
