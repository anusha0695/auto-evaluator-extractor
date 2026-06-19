"""Row matching — pair GT rows with extraction rows using identity keys.

Strategy: match-against-both (no Section column on the GT XLSX).
  1. For each GT row, build the candidate identity tuple under the variants
     section (gene_studied, amino_acid_change) and the biomarkers section
     (biomarker_name).
  2. For each extraction row, build the same identity tuple from its actual
     section.
  3. Try to pair: GT-as-variant ↔ extraction[variant]; if that fails, try
     GT-as-biomarker ↔ extraction[biomarker].
  4. Anything unpaired is reported as missing (in GT) or spurious (in extraction).

All comparisons use canonical() so SME shorthand (`MSI`, `G12D`, `HER2`)
matches verbatim extraction ("MICROSATELLITE INSTABILITY", "p.G12D", "ERBB2").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evaluation.lib.canonical import canonical
from evaluation.lib.load_extraction import ExtractedRow
from evaluation.lib.load_ground_truth import GTRow


@dataclass
class RowPair:
    section: str                # "variants" | "biomarkers"
    gt: GTRow | None            # None when extracted row has no GT counterpart (spurious)
    extracted: ExtractedRow | None  # None when GT row has no extracted counterpart (missing)
    identity: tuple             # for diagnostics (the canonical identity tuple)


# Column headers used to populate the identity tuples for each section.
# These come from the lab workbook layout; we don't re-declare them in the
# field_map yaml because that would duplicate identity_keys.
_GT_BIOMARKER_COL = "Biomarker"
_GT_AA_CHANGE_COL = "Amino Acid Change"


def _gt_identity(row: GTRow, section: str) -> tuple | None:
    """Build the identity tuple from a GT row for the given candidate section.

    Variant identity is `(gene, amino_acid_change)` when both are present, OR
    `(gene,)` alone for gene-keyed rows that legitimately have no HGVS change
    (HLA typing, pharmacogenomic genes like DPYD/UGT1A1/CYP2D6 — anything
    using non-HGVS nomenclature). Requiring both fields used to collapse all
    such rows onto a single shared identity, which then collided in
    render_report.by_row and silently dropped every record but the last.
    """
    if section == "variants":
        gene = canonical(row.cells.get(_GT_BIOMARKER_COL))
        aa = canonical(row.cells.get(_GT_AA_CHANGE_COL))
        if not gene:
            return None
        if not aa:
            return (gene,)          # gene-only fallback (HLA, DPYD, …)
        return (gene, aa)
    if section == "biomarkers":
        name = canonical(row.cells.get(_GT_BIOMARKER_COL))
        if not name:
            return None
        return (name,)
    return None


def _extracted_identity(row: ExtractedRow) -> tuple | None:
    """Build the identity tuple from an extracted row, using the section the
    JSON already declared. Same gene-only fallback as `_gt_identity` so the
    two sides stay symmetric (a GT row 'HLA-A' pairs with the extracted
    'HLA-A' on identity `('HLA-A',)`)."""
    if row.section == "variants":
        gene = canonical(row.cells.get(_GT_BIOMARKER_COL))
        aa = canonical(row.cells.get(_GT_AA_CHANGE_COL))
        if not gene:
            return None
        if not aa:
            return (gene,)          # gene-only fallback (HLA, DPYD, …)
        return (gene, aa)
    if row.section == "biomarkers":
        name = canonical(row.cells.get(_GT_BIOMARKER_COL))
        if not name:
            return None
        return (name,)
    return None


def match(gt_rows: list[GTRow], ex_rows: list[ExtractedRow]) -> list[RowPair]:
    """Return one RowPair per GT row, per extracted row, or per (gt,ex) match.

    The output:
      • matched pairs    → (gt=GT, extracted=EX, section=…)
      • missing-in-ex   → (gt=GT, extracted=None, section=best-guess from cells)
      • spurious-in-ex   → (gt=None, extracted=EX, section=row.section)
    """
    # Index extracted rows by their section-specific identity.
    ex_index_var: dict[tuple, ExtractedRow] = {}
    ex_index_bio: dict[tuple, ExtractedRow] = {}
    unindexed_ex: list[ExtractedRow] = []
    for er in ex_rows:
        ident = _extracted_identity(er)
        if ident is None:
            unindexed_ex.append(er)
            continue
        if er.section == "variants":
            ex_index_var.setdefault(ident, er)
        else:
            ex_index_bio.setdefault(ident, er)

    matched_ex_ids: set[int] = set()
    pairs: list[RowPair] = []

    # Walk GT rows — try variant identity first, then biomarker.
    for gr in gt_rows:
        var_id = _gt_identity(gr, "variants")
        bio_id = _gt_identity(gr, "biomarkers")
        if var_id and var_id in ex_index_var:
            er = ex_index_var[var_id]
            matched_ex_ids.add(id(er))
            gr.candidate_section = "variants"
            pairs.append(RowPair(section="variants", gt=gr, extracted=er, identity=var_id))
            continue
        if bio_id and bio_id in ex_index_bio:
            er = ex_index_bio[bio_id]
            matched_ex_ids.add(id(er))
            gr.candidate_section = "biomarkers"
            pairs.append(RowPair(section="biomarkers", gt=gr, extracted=er, identity=bio_id))
            continue

        # GT not matched — decide best-guess section for missing-bucket placement.
        # Use which identity tuple was resolvable; if both, prefer variants (more specific).
        if var_id:
            gr.candidate_section = "variants"
            pairs.append(RowPair(section="variants", gt=gr, extracted=None, identity=var_id))
        elif bio_id:
            gr.candidate_section = "biomarkers"
            pairs.append(RowPair(section="biomarkers", gt=gr, extracted=None, identity=bio_id))
        else:
            # Could not resolve any identity — report as biomarker missing with
            # whatever raw Biomarker cell text we have, so the SME can fix it.
            # Use the sheet row number as a last-resort uniqifier so multiple
            # blank-Biomarker rows don't collide on the same identity.
            raw = gr.cells.get(_GT_BIOMARKER_COL) or ""
            canon_raw = canonical(raw) if raw else ""
            ident = (canon_raw or f"GT-row-{gr.sheet_row}",)
            gr.candidate_section = "biomarkers"
            pairs.append(RowPair(section="biomarkers", gt=gr, extracted=None,
                                  identity=ident))

    # Spurious — any extracted row that didn't get matched above. Each row
    # MUST get a unique identity so render_report's by_row dict does not
    # collapse multiple records onto the same key (the bug that made
    # gene-only HLA/DPYD records vanish from the Review Results sheet).
    for er in ex_rows:
        if id(er) in matched_ex_ids:
            continue
        ident = _extracted_identity(er)
        if ident is None:
            # Last-resort: canonical(Biomarker) if present, else a
            # per-record tag derived from the JSON index. Both keep each
            # unresolved row in its own by_row bucket.
            raw = er.cells.get(_GT_BIOMARKER_COL) or ""
            canon_raw = canonical(raw) if raw else ""
            ident = (canon_raw or f"{er.section}-row-{er.record_index}",)
        pairs.append(RowPair(section=er.section, gt=None, extracted=er, identity=ident))

    return pairs
