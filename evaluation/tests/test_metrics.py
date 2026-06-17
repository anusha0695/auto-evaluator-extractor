"""Sanity tests for the metric computation — synthetic pairs, no disk I/O."""

from evaluation.lib.load_ground_truth import GTRow
from evaluation.lib.load_extraction import ExtractedRow
from evaluation.lib.match_rows import RowPair
from evaluation.lib.metrics import compute


_COLUMNS = [
    {"header": "Biomarker"},
    {"header": "Method"},
    {"header": "Result"},
    {"header": "Variant Allele Freq"},
]


def _gt(biomarker, method, result, vaf=None):
    return GTRow(cells={"Biomarker": biomarker, "Method": method, "Result": result,
                        "Variant Allele Freq": vaf})


def _ex(section, biomarker, method, result, vaf=None):
    return ExtractedRow(section=section, cells={
        "Biomarker": biomarker, "Method": method, "Result": result,
        "Variant Allele Freq": vaf,
    })


def test_all_match_perfect_F1():
    g = _gt("KRAS", "Seq", "Pathogenic Variant", "10")
    e = _ex("variants", "KRAS", "Seq", "Pathogenic Variant", "10")
    pairs = [RowPair(section="variants", gt=g, extracted=e, identity=("KRAS",))]
    m = compute(pairs, _COLUMNS)
    assert m["overall"]["f1"] == 1.0
    assert m["overall"]["precision"] == 1.0
    assert m["overall"]["recall"] == 1.0
    assert m["row_level"]["variants"]["matched"] == 1


def test_missing_row_drops_recall():
    g = _gt("KRAS", "Seq", "Pathogenic", "10")
    pairs = [RowPair(section="variants", gt=g, extracted=None, identity=("KRAS",))]
    m = compute(pairs, _COLUMNS)
    assert m["row_level"]["variants"]["missing"] == 1
    assert m["row_level"]["variants"]["recall"] == 0.0


def test_spurious_row_drops_precision():
    e = _ex("variants", "DPYD", None, None, None)
    pairs = [RowPair(section="variants", gt=None, extracted=e, identity=("DPYD",))]
    m = compute(pairs, _COLUMNS)
    assert m["row_level"]["variants"]["spurious"] == 1
    assert m["row_level"]["variants"]["precision"] == 0.0


def test_wrong_cell_appears_in_field_metrics():
    g = _gt("KRAS", "Seq", "Pathogenic", "10")
    e = _ex("variants", "KRAS", "NGS", "Pathogenic", "10")        # Method differs
    pairs = [RowPair(section="variants", gt=g, extracted=e, identity=("KRAS",))]
    m = compute(pairs, _COLUMNS)
    # Method should have a Wrong count.
    assert m["field_level"]["Method"]["wrong"] == 1
    # Method precision and recall drop because WRONG is both an effective FP and FN.
    assert m["field_level"]["Method"]["precision"] < 1.0
    assert m["field_level"]["Method"]["recall"] < 1.0
    # Biomarker, Result, VAF all matched.
    assert m["field_level"]["Biomarker"]["f1"] == 1.0
    assert m["field_level"]["Result"]["f1"] == 1.0
    assert m["field_level"]["Variant Allele Freq"]["f1"] == 1.0
