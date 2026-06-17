"""Tests for the strict-on-canonical comparator and the canonicalizer."""

from evaluation.lib.canonical import canonical
from evaluation.lib.compare_cells import compare, TP, WRONG, FN, FP, TN


def test_empty_both_is_TN():
    assert compare(None, None).verdict == TN
    assert compare("", "").verdict == TN
    assert compare("  ", None).verdict == TN
    assert compare(None, "nan").verdict == TN


def test_gt_only_is_FN():
    assert compare("Somatic", None).verdict == FN
    assert compare("Somatic", "").verdict == FN


def test_extracted_only_is_FP():
    assert compare(None, "Somatic").verdict == FP
    assert compare("", "Somatic").verdict == FP


def test_exact_match_is_TP():
    assert compare("Somatic", "Somatic").verdict == TP
    assert compare("Somatic", "  Somatic  ").verdict == TP   # whitespace tolerated


def test_HGVS_prefix_match_is_TP():
    # GT shorthand: "G12D"  ↔  extraction: "p.G12D"  → strip prefix → equal
    assert compare("G12D", "p.G12D").verdict == TP
    assert compare("c.35G>A", "35G>A").verdict == TP


def test_VAF_strict_numeric_strip_pct():
    # `10` == `10%`  (unit omission tolerated)
    assert compare("10", "10%").verdict == TP
    assert compare("10%", "10").verdict == TP
    # `9.8` ≠ `10`  (precision drift surfaces)
    assert compare("9.8%", "10%").verdict == WRONG
    assert compare("9.8", "10").verdict == WRONG


def test_canonical_idempotent_on_empty():
    assert canonical(None) == ""
    assert canonical("") == ""
    assert canonical("   ") == ""


def test_canonical_HGVS_strips_prefix():
    assert canonical("p.G12D") == canonical("G12D")
    assert canonical("c.35G>A") == canonical("35G>A")


def test_canonical_numeric_VAF_strips_pct():
    assert canonical("10%") == "10"
    assert canonical("10") == "10"
    assert canonical("9.8%") == "9.8"
    assert canonical("9.8") == "9.8"
