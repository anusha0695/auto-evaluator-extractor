#!/usr/bin/env python3
"""
score_against_ground_truth.py — diff a pipeline extraction against the
matching `ground_truth/*.json` and report per-field pass/fail.

Phase 1 scored only `report_metadata`. Phase 2 scores all v3 sections:
`report_metadata`, `other_molecular_biomarker_umbrella` (THE biomarker umbrella
— v3 merge folded sequence variants in here as findings with a nested
`variant_detail`), `tested_biomarker_umbrella`, `significant_findings`, and
`clinical_information`.

Per-field comparison policy (highest precedence first):

  - Both null               → pass (correctly omitted)
  - Strict equality         → pass (exact match)
  - Date fields             → pass if both parse to the same ISO date
  - String fields           → pass if equal after lowercase + whitespace
                              normalize (e.g. "NEOGENOMICS" vs "NeoGenomics")
  - Number / int            → pass on exact equality
  - Otherwise               → fail

`llm_confidence_score` is excluded from scoring — it's a runtime quality
self-report, not an extraction target.

Usage:
    python scripts/score_against_ground_truth.py --doc demo \\
        --extraction-file local_extraction.json

    python scripts/score_against_ground_truth.py --doc demo \\
        --from-bigquery

Exit codes:
    0 — pass rate ≥ threshold (default 0.80)
    1 — pass rate <  threshold
    2 — failed to load extraction or ground truth
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make repo-root imports work regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env into os.environ for the --from-bigquery code path.
import core.env_loader  # noqa: E402, F401

logger = logging.getLogger("score")


# ---------------------------------------------------------------------------
# Field-level comparison
# ---------------------------------------------------------------------------

DATE_FIELDS = {
    "Patient_DOB",
    "Collection_Date",
    "Received_Date",
    "Report_Date",
}

# Fields excluded from scoring — they're meta-data, not extraction targets.
EXCLUDED_FIELDS = {
    "llm_confidence_score",   # model self-report
    "provenance",              # per-field source citations (Option B); UI consumes it
}

# Long free-text / narrative fields: exact match is unfair (any truncation or
# whitespace diff fails). Scored by OVERLAP — pass if one normalized string
# contains the other OR token-Jaccard ≥ NARRATIVE_OVERLAP_MIN.
NARRATIVE_FIELDS = {
    "clinical_significance", "interpretation", "reason_for_study",
    "clinical_finding_details", "gross_description", "microscopic_description",
    "text", "details",
}
NARRATIVE_OVERLAP_MIN = 0.6

# Fields where strict equality is required (no normalization).
STRICT_FIELDS = {
    "total_pages",
    "Ordering_Provider_NPI",
    "Practice_NPI",
    "Practice_ZIP_5digit",
    "Practice_ZIP_4digit",
}


@dataclass
class FieldComparison:
    field_name: str
    expected: Any
    actual: Any
    passed: bool
    match_type: str   # "exact" | "both_null" | "date" | "normalized_string" | "fail"
    notes: str = ""


@dataclass
class ScoreReport:
    doc_id: str
    section_name: str
    comparisons: list[FieldComparison] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.comparisons)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.comparisons if c.passed)

    @property
    def pass_rate(self) -> float:
        if not self.total:
            return 0.0
        return self.passed / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "section": self.section_name,
            "total_fields": self.total,
            "passed": self.passed,
            "failed": self.total - self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "comparisons": [
                {
                    "field": c.field_name,
                    "expected": c.expected,
                    "actual": c.actual,
                    "passed": c.passed,
                    "match_type": c.match_type,
                    "notes": c.notes,
                }
                for c in self.comparisons
            ],
        }


def _normalize_string(s: str) -> str:
    """Lowercase + collapse whitespace, strip surrounding whitespace."""
    return re.sub(r"\s+", " ", s.strip()).lower()


def _try_parse_date_iso(value: Any) -> str | None:
    """Return value's ISO YYYY-MM-DD form if parseable, else None."""
    if not isinstance(value, str):
        return None
    try:
        from dateutil import parser as _dp

        # Strip trailing sex/TZ fragments — same logic as the date_parser tool.
        cleaned = re.sub(r"\s*/\s*[MFmf]\s*$", "", value.strip())
        dt = _dp.parse(cleaned, fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return None


def _compare_field(name: str, expected: Any, actual: Any) -> FieldComparison:
    """Compare one field, returning a FieldComparison."""
    # Both null → pass
    if expected is None and actual is None:
        return FieldComparison(name, expected, actual, True, "both_null")

    # One null, other not → fail
    if expected is None or actual is None:
        return FieldComparison(
            name, expected, actual, False, "fail",
            notes=f"one side null (expected={expected!r}, actual={actual!r})",
        )

    # Strict equality always wins (catches the common case quickly)
    if expected == actual:
        return FieldComparison(name, expected, actual, True, "exact")

    # Fields where only strict equality is allowed
    if name in STRICT_FIELDS:
        return FieldComparison(
            name, expected, actual, False, "fail",
            notes=f"strict field — expected exact match",
        )

    # Date fields → parse + compare
    if name in DATE_FIELDS:
        exp_iso = _try_parse_date_iso(expected)
        act_iso = _try_parse_date_iso(actual)
        if exp_iso and act_iso and exp_iso == act_iso:
            return FieldComparison(
                name, expected, actual, True, "date",
                notes=f"matched after ISO normalization ({exp_iso})",
            )
        return FieldComparison(
            name, expected, actual, False, "fail",
            notes=f"date mismatch (expected_iso={exp_iso}, actual_iso={act_iso})",
        )

    # Narrative / long-text → overlap match (containment or token-Jaccard)
    if name in NARRATIVE_FIELDS and isinstance(expected, str) and isinstance(actual, str):
        e, a = _normalize_string(expected), _normalize_string(actual)
        if e == a or e in a or a in e:
            return FieldComparison(name, expected, actual, True, "narrative_contains",
                                   notes="one narrative string contains the other")
        te, ta = set(e.split()), set(a.split())
        jac = len(te & ta) / len(te | ta) if (te | ta) else 0.0
        if jac >= NARRATIVE_OVERLAP_MIN:
            return FieldComparison(name, expected, actual, True, "narrative_overlap",
                                   notes=f"token overlap {jac:.2f} ≥ {NARRATIVE_OVERLAP_MIN}")
        return FieldComparison(name, expected, actual, False, "fail",
                               notes=f"narrative overlap {jac:.2f} < {NARRATIVE_OVERLAP_MIN}")

    # Strings → normalized compare
    if isinstance(expected, str) and isinstance(actual, str):
        if _normalize_string(expected) == _normalize_string(actual):
            return FieldComparison(
                name, expected, actual, True, "normalized_string",
                notes="matched after lowercase + whitespace normalize",
            )
        return FieldComparison(
            name, expected, actual, False, "fail",
            notes="string mismatch even after normalization",
        )

    return FieldComparison(
        name, expected, actual, False, "fail",
        notes=f"type mismatch ({type(expected).__name__} vs {type(actual).__name__})",
    )


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------


def score_report_metadata(
    *,
    doc_id: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> ScoreReport:
    """Diff the two report_metadata payloads field by field."""
    report = ScoreReport(doc_id=doc_id, section_name="report_metadata")

    # Score every field in the ground truth.
    for field_name in expected:
        if field_name in EXCLUDED_FIELDS:
            continue
        cmp = _compare_field(field_name, expected[field_name], actual.get(field_name))
        report.comparisons.append(cmp)

    # NOTE: we do NOT penalize "extra" fields the extractor emitted that aren't
    # in the ground truth. `report_metadata` is validated with
    # additionalProperties:false, so the model CANNOT emit a hallucinated key —
    # any extra field is a valid (newer) schema field the GT simply hasn't been
    # labelled for yet. Penalizing it would regress every fixture each time the
    # schema grows (e.g. the production-parity Accession_Number / Report_Type
    # additions). Hallucinated keys are caught by the schema validator, not here.

    return report


# ---------------------------------------------------------------------------
# Phase 2 — multi-section scoring (set / array / object), shape-tolerant
# ---------------------------------------------------------------------------


@dataclass
class SectionScore:
    """One section's score. For object sections, P/R mirror field pass-rate;
    for array/set sections, P/R are item-level."""
    section: str
    kind: str                       # "object" | "array" | "set"
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    expected_count: int = 0
    actual_count: int = 0
    matched: int = 0
    field_pass_rate: float | None = None   # mean field accuracy over matched items
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "section": self.section, "kind": self.kind,
            "precision": round(self.precision, 4), "recall": round(self.recall, 4),
            "f1": round(self.f1, 4), "expected_count": self.expected_count,
            "actual_count": self.actual_count, "matched": self.matched, "notes": self.notes,
        }
        if self.field_pass_rate is not None:
            d["field_pass_rate"] = round(self.field_pass_rate, 4)
        return d


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else (1.0 if fn == 0 else 0.0)
    r = tp / (tp + fn) if (tp + fn) else (1.0 if fp == 0 else 0.0)
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def _gene_key(symbol: Any) -> str:
    """HGNC-normalized lowercase key for matching genes (alias-tolerant)."""
    s = (symbol or "")
    if not isinstance(s, str) or not s.strip():
        return ""
    try:
        from preprocess.hgnc_resolver import hgnc_normalize
        canon = hgnc_normalize(s).get("canonical")
        if canon:
            return canon.lower()
    except Exception:
        pass
    return _normalize_string(s)


def score_set_section(section: str, expected: list[Any], actual: list[Any], *, gene_keyed: bool) -> SectionScore:
    """Set-membership P/R (e.g. tested_biomarkers panel)."""
    keyfn = _gene_key if gene_keyed else (lambda x: _normalize_string(x) if isinstance(x, str) else str(x))
    exp = {keyfn(x) for x in (expected or []) if keyfn(x)}
    act = {keyfn(x) for x in (actual or []) if keyfn(x)}
    tp = len(exp & act); fp = len(act - exp); fn = len(exp - act)
    p, r, f = _prf(tp, fp, fn)
    return SectionScore(section, "set", p, r, f, len(exp), len(act), tp,
                        notes=f"tp={tp} fp={fp} fn={fn}")


def score_object_array(
    section: str, expected: list[dict], actual: list[dict], *,
    key_fields: list[str], score_fields: list[str], gene_field: str | None = None,
) -> SectionScore:
    """Identity-match array items by key, then field-accuracy over matched pairs.
    Item-level P/R from matches; unmatched expected = FN, unmatched actual = FP."""
    def item_key(it: dict) -> tuple:
        parts = []
        for kf in key_fields:
            v = it.get(kf)
            if gene_field and kf == gene_field:
                parts.append(_gene_key(v))
            else:
                parts.append(_normalize_string(v) if isinstance(v, str) else v)
        return tuple(parts)

    exp_by = {item_key(x): x for x in (expected or [])}
    act_by = {item_key(x): x for x in (actual or [])}
    matched_keys = set(exp_by) & set(act_by)
    tp, fp, fn = len(matched_keys), len(set(act_by) - set(exp_by)), len(set(exp_by) - set(act_by))
    p, r, f = _prf(tp, fp, fn)

    # field accuracy over matched items
    field_total = field_pass = 0
    for k in matched_keys:
        e, a = exp_by[k], act_by[k]
        for fld in score_fields:
            if fld in EXCLUDED_FIELDS:
                continue
            field_total += 1
            if _compare_field(fld, e.get(fld), a.get(fld)).passed:
                field_pass += 1
    fpr = (field_pass / field_total) if field_total else None
    return SectionScore(section, "array", p, r, f, len(exp_by), len(act_by), tp,
                        field_pass_rate=fpr, notes=f"tp={tp} fp={fp} fn={fn}")


def score_object_section(section: str, expected: dict, actual: dict, *, fields: list[str]) -> SectionScore:
    """Field-by-field object scoring (e.g. clinical_information scalars)."""
    total = passed = 0
    for fld in fields:
        if fld in EXCLUDED_FIELDS:
            continue
        total += 1
        if _compare_field(fld, expected.get(fld), actual.get(fld)).passed:
            passed += 1
    rate = (passed / total) if total else 1.0
    return SectionScore(section, "object", rate, rate, rate, total, total, passed,
                        field_pass_rate=rate, notes=f"{passed}/{total} fields")


# v3 merge: variants are biomarker findings with a nested variant_detail.
# These detail fields are flattened onto the finding row and scored alongside
# result/interpretation (both-null counts as a match, so IHC findings with no
# variant_detail aren't penalized).
_VARIANT_DETAIL_FIELDS = [
    "variant_allele_frequency", "coding_dna_change", "amino_acid_change",
    "clinical_significance", "genomic_source_class", "exon",
]

# v4 (D1): the revived Genomic_Variant_umbrella scores its FLAT verbatim variant
# fields. gene_studied + coding/amino change are the match KEY (scored implicitly by
# the match), so they're not repeated here; these are the value fields.
_VARIANT_SCORE_FIELDS = [
    "method", "result", "variant_allele_frequency", "genomic_dna_change",
    "clinical_significance", "genomic_source_class", "allelic_state",
    "chromosome_identifier", "exon", "dna_change_type", "amino_acid_change_type",
]


def score_all_sections(doc_id: str, gt_env: dict[str, Any], ex_env: dict[str, Any]) -> list[SectionScore]:
    """Score every section that has non-empty ground truth. v2/v3 shape-tolerant:
    other_molecular flat (v2 gt) vs findings[] (v3 output) is reconciled by name."""
    scores: list[SectionScore] = []

    # report_metadata (reuse the field scorer for parity with Phase 1)
    gt_md, ex_md = gt_env.get("report_metadata"), ex_env.get("report_metadata")
    if isinstance(gt_md, dict):
        rep = score_report_metadata(doc_id=doc_id, expected=gt_md, actual=ex_md or {})
        scores.append(SectionScore(
            "report_metadata", "object", rep.pass_rate, rep.pass_rate, rep.pass_rate,
            rep.total, rep.total, rep.passed, field_pass_rate=rep.pass_rate,
            notes=f"{rep.passed}/{rep.total} fields"))

    # tested_biomarker_umbrella (set)
    gt_t = (gt_env.get("tested_biomarker_umbrella") or {}).get("tested_biomarkers")
    if gt_t:
        ex_t = (ex_env.get("tested_biomarker_umbrella") or {}).get("tested_biomarkers") or []
        scores.append(score_set_section("tested_biomarker_umbrella", gt_t, ex_t, gene_keyed=True))

    # Genomic_Variant_umbrella (v4 — revived SEPARATE sequence-variant section; array).
    # Additive: v3 envelopes have no Genomic_Variant_umbrella, so this block is skipped
    # and v2/v3 scoring is unchanged. Items match on gene (HGNC-alias-tolerant) + the
    # printed coding/amino change, then field-accuracy over the verbatim variant fields.
    gt_v = (gt_env.get("Genomic_Variant_umbrella") or {}).get("Genomic_Variants")
    if gt_v:
        ex_v = (ex_env.get("Genomic_Variant_umbrella") or {}).get("Genomic_Variants") or []
        scores.append(score_object_array(
            "Genomic_Variant_umbrella", gt_v, ex_v,
            key_fields=["gene_studied", "coding_dna_change", "amino_acid_change"],
            score_fields=_VARIANT_SCORE_FIELDS, gene_field="gene_studied"))

    # other_molecular_biomarker_umbrella — flatten v3 findings[] to (name,result) pairs
    gt_b = (gt_env.get("other_molecular_biomarker_umbrella") or {}).get("other_molecular_biomarkers")
    if gt_b:
        ex_b = (ex_env.get("other_molecular_biomarker_umbrella") or {}).get("other_molecular_biomarkers") or []
        scores.append(score_object_array(
            "other_molecular_biomarker_umbrella",
            _flatten_biomarkers(gt_b), _flatten_biomarkers(ex_b),
            key_fields=["biomarker_name", "method"],
            score_fields=["result", "interpretation", "reference_range", "biomarker_class"] + _VARIANT_DETAIL_FIELDS,
            # Match biomarker names through the same HGNC alias canonicalizer used
            # for the gene panel, so synonym pairs (HER2 ≡ HER2/neu ≡ ERBB2,
            # ER ≡ ESR1, PR ≡ PGR) align without a fixture-specific lookup.
            gene_field="biomarker_name"))

    # clinical_information (object scalars)
    gt_c = gt_env.get("clinical_information")
    if isinstance(gt_c, dict) and any(gt_c.get(k) for k in ("reason_for_study", "clinical_finding_details")):
        ex_c = ex_env.get("clinical_information") or {}
        scores.append(score_object_section(
            "clinical_information", gt_c, ex_c,
            fields=["reason_for_study", "clinical_finding_details", "number_of_clinical_histories"]))

    # significant_findings (Phase 2b) — match specimens by specimen_id, score
    # the per-specimen fields (gross/micro narrative, pTNM + staging version,
    # lymph nodes, histology).
    gt_sf = (gt_env.get("significant_findings") or {}).get("specimen_findings")
    if gt_sf:
        ex_sf = (ex_env.get("significant_findings") or {}).get("specimen_findings") or []
        scores.append(score_object_array(
            "significant_findings",
            [_flatten_specimen(s) for s in gt_sf], [_flatten_specimen(s) for s in ex_sf],
            key_fields=["specimen_id"], score_fields=_SPECIMEN_SCORE_FIELDS))

    return scores


_SPECIMEN_SCORE_FIELDS = [
    "tissue_type", "laterality", "procedure", "gross_description",
    "microscopic_description", "histologic", "pTNM_stage",
    "staging_system_version", "lymph_node_status",
    "number_of_lymph_nodes_examined", "number_of_lymph_nodes_positive",
]


def _flatten_specimen(sf: dict) -> dict:
    """Flatten one specimen_findings entry to a comparable record keyed by the
    (first) specimen_id, pulling the nested scalar/narrative fields up."""
    sp = (sf.get("specimen") or [{}])[0] if (sf.get("specimen")) else {}
    pd = sf.get("procedure_details") or {}
    gd = sf.get("gross_description") or {}
    md = sf.get("microscopic_description") or {}
    tn = sf.get("pTNM_staging_details") or {}
    ln = sf.get("lymph_node_details") or {}
    hist = " | ".join(sorted(
        (f.get("finding") or "") for f in (sf.get("histologic_findings") or []) if f.get("finding")))
    return {
        "specimen_id": sp.get("specimen_id"),
        "tissue_type": sp.get("tissue_type"),
        "laterality": sp.get("laterality"),
        "procedure": pd.get("procedure"),
        "gross_description": gd.get("text"),
        "microscopic_description": md.get("text"),
        "histologic": hist or None,
        "pTNM_stage": tn.get("pTNM_stage"),
        "staging_system_version": tn.get("staging_system_version"),
        "lymph_node_status": ln.get("lymph_node_status"),
        "number_of_lymph_nodes_examined": ln.get("number_of_lymph_nodes_examined"),
        "number_of_lymph_nodes_positive": ln.get("number_of_lymph_nodes_positive"),
    }


def _flatten_biomarkers(items: list[dict]) -> list[dict]:
    """Normalize a biomarker list to (biomarker_name, method, result, interpretation
    + variant_detail subfields) rows, accepting both v2 flat shape and v3 nested
    findings[]. v3 merge: a finding that is a sequence variant carries a nested
    variant_detail — its fields are lifted onto the row so they score alongside
    result/interpretation."""
    def _row(name, cls, f):
        row = {"biomarker_name": name, "biomarker_class": cls,
               "method": f.get("method"),
               "result": f.get("result"), "interpretation": f.get("interpretation")}
        vd = f.get("variant_detail") or {}
        for k in _VARIANT_DETAIL_FIELDS:
            row[k] = vd.get(k) if isinstance(vd, dict) else None
        return row
    out: list[dict] = []
    for bm in (items or []):
        name = bm.get("biomarker_name")
        cls = bm.get("biomarker_class")
        findings = bm.get("findings")
        if isinstance(findings, list) and findings:                       # v3 nested
            for f in findings:
                out.append(_row(name, cls, f))
        else:                                                              # v2 flat
            out.append(_row(name, cls, bm))
    return out


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_ground_truth(doc_id: str) -> dict[str, Any]:
    """Find the matching ground_truth/*.json. Tries primary then phase1/."""
    candidates = [
        _REPO_ROOT / "ground_truth" / f"{doc_id}.json",
        _REPO_ROOT / "ground_truth" / "phase1" / f"{doc_id}.json",
    ]
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            metadata = data.get("genomic_pathology_extraction", {}).get("report_metadata")
            if metadata is None:
                raise ValueError(
                    f"{path} loaded but has no "
                    f"`genomic_pathology_extraction.report_metadata` section"
                )
            logger.info("ground truth loaded from %s", path)
            return metadata
    raise FileNotFoundError(
        f"No ground truth found for doc_id={doc_id!r}. Tried: "
        f"{', '.join(str(p) for p in candidates)}"
    )


def load_extraction_file(path: str | Path) -> dict[str, Any]:
    """Read an extraction JSON from disk. Accepts either a full envelope
    `{genomic_pathology_extraction: {...}}` or just the inner extraction
    dict."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Extraction file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    # Accept both shapes.
    if "genomic_pathology_extraction" in data:
        envelope = data["genomic_pathology_extraction"]
    else:
        envelope = data
    metadata = envelope.get("report_metadata")
    if metadata is None:
        raise ValueError(
            f"{p} loaded but has no report_metadata (under top-level "
            "`genomic_pathology_extraction.report_metadata` or as a direct field)."
        )
    return metadata


def load_ground_truth_envelope(doc_id: str) -> dict[str, Any]:
    """Return the FULL ground-truth envelope (all sections), not just metadata."""
    candidates = [
        _REPO_ROOT / "ground_truth" / f"{doc_id}.json",
        _REPO_ROOT / "ground_truth" / "phase1" / f"{doc_id}.json",
    ]
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            env = data.get("genomic_pathology_extraction", data)
            logger.info("ground truth envelope loaded from %s", path)
            return env
    raise FileNotFoundError(
        f"No ground truth found for doc_id={doc_id!r}. Tried: "
        f"{', '.join(str(p) for p in candidates)}"
    )


def load_extraction_envelope_file(path: str | Path) -> dict[str, Any]:
    """Return the FULL extraction envelope from disk. Accepts: a process_local
    --json dump (top-level `extraction`), a `{genomic_pathology_extraction:{}}`
    wrapper, or a bare envelope dict."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Extraction file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data.get("extraction"), dict):
        return data["extraction"]
    if isinstance(data.get("genomic_pathology_extraction"), dict):
        return data["genomic_pathology_extraction"]
    return data


async def load_extraction_from_bigquery(doc_id: str) -> dict[str, Any]:
    """Fetch the most-recent extraction row from BigQuery for this doc_id."""
    from core.persistence import load_storage_config

    cfg = load_storage_config("phase_1")
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-bigquery not installed — install Phase 1 deps "
            "(`make install`) before using --from-bigquery."
        ) from exc

    client = bigquery.Client(project=cfg.bigquery_project)
    query = (
        f"SELECT genomic_pathology_extraction "
        f"FROM `{cfg.extractions_table_fqn}` "
        f"WHERE doc_id = @doc_id "
        f"ORDER BY extracted_at DESC LIMIT 1"
    )
    job = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("doc_id", "STRING", doc_id)]
        ),
    )
    rows = list(job.result())
    if not rows:
        raise RuntimeError(
            f"No BigQuery extraction row found for doc_id={doc_id!r} in "
            f"{cfg.extractions_table_fqn}. Run the pipeline first via "
            f"`make run-local PDF=gs://.../{doc_id}.pdf`."
        )
    raw_json = rows[0]["genomic_pathology_extraction"]
    envelope = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    metadata = envelope.get("report_metadata")
    if metadata is None:
        raise ValueError(
            f"BigQuery row has no report_metadata under "
            "`genomic_pathology_extraction.report_metadata`."
        )
    return metadata


def load_extraction_from_local(doc_id: str) -> dict[str, Any]:
    """Fetch the most-recent extraction row from `local_runs/extractions_v1.jsonl`."""
    from core.persistence import load_storage_config

    cfg = load_storage_config("phase_1")
    jsonl_path = cfg.local_dir_resolved / "extractions_v1.jsonl"
    if not jsonl_path.exists():
        raise RuntimeError(
            f"Local extractions file not found: {jsonl_path}. "
            f"Run the pipeline first via `make run-local PDF=...`."
        )

    # Scan the file for the doc — last matching line wins (most recent).
    matches: list[dict[str, Any]] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("doc_id") == doc_id:
                matches.append(row)

    if not matches:
        raise RuntimeError(
            f"No local extraction row found for doc_id={doc_id!r} in {jsonl_path}. "
            f"Run the pipeline first via `make run-local PDF=...`."
        )
    row = matches[-1]   # most recent (file is append-only)
    raw_json = row.get("genomic_pathology_extraction")
    envelope = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    metadata = envelope.get("report_metadata") if envelope else None
    if metadata is None:
        raise ValueError(
            f"Local row {row.get('extraction_id')} has no report_metadata under "
            "`genomic_pathology_extraction.report_metadata`."
        )
    logger.info(
        "loaded extraction from %s (extraction_id=%s)", jsonl_path, row.get("extraction_id")
    )
    return metadata


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


GREEN = "\033[1;32m"
RED = "\033[1;31m"
YELLOW = "\033[1;33m"
DIM = "\033[2m"
RESET = "\033[0m"


def _print_table(report: ScoreReport, *, verbose: bool) -> None:
    """Print a per-field comparison table to stdout."""
    print(f"\n{'=' * 78}")
    print(f"Doc: {report.doc_id}   Section: {report.section_name}")
    print(f"{'=' * 78}")

    name_w = max((len(c.field_name) for c in report.comparisons), default=20)

    for c in report.comparisons:
        mark = f"{GREEN}✓{RESET}" if c.passed else f"{RED}✗{RESET}"
        expected_repr = _truncate(repr(c.expected), 28)
        actual_repr = _truncate(repr(c.actual), 28)
        line = f"  {mark}  {c.field_name:<{name_w}}  expected={expected_repr:<28}  actual={actual_repr}"
        print(line)
        if (not c.passed or verbose) and c.notes:
            color = DIM if c.passed else YELLOW
            print(f"     {color}{c.notes}{RESET}")
        if not c.passed and c.match_type != "fail":
            # Render the soft-match-type for non-strict passes
            pass

    rate = report.pass_rate * 100
    color = GREEN if report.pass_rate >= 0.80 else (YELLOW if report.pass_rate >= 0.60 else RED)
    print(f"\n  {color}Pass rate: {report.passed}/{report.total} = {rate:.1f}%{RESET}")


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _print_section_scores(doc_id: str, scores: list[SectionScore]) -> None:
    """Per-section P/R/F1 table (PHI-safe — counts + rates only, no values)."""
    print(f"\n{'=' * 78}")
    print(f"Doc: {doc_id}   Phase 2a multi-section score")
    print(f"{'=' * 78}")
    print(f"  {'section':<38} {'kind':<7} {'P':>5} {'R':>5} {'F1':>5}  exp/act/match  fields")
    print(f"  {'-' * 74}")
    for s in scores:
        col = GREEN if s.f1 >= 0.80 else (YELLOW if s.f1 >= 0.60 else RED)
        fpr = f"{s.field_pass_rate:.2f}" if s.field_pass_rate is not None else "  - "
        print(f"  {s.section:<38} {s.kind:<7} {col}{s.precision:5.2f} {s.recall:5.2f} "
              f"{s.f1:5.2f}{RESET}  {s.expected_count:>3}/{s.actual_count:>3}/{s.matched:<3}   {fpr}")
    if not scores:
        print(f"  {YELLOW}(no sections with ground truth found){RESET}")


# ---------------------------------------------------------------------------
# argparse + main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Score a pipeline extraction against the matching ground_truth JSON. "
            "Phase 1: report_metadata section only."
        ),
        epilog=(
            "Examples:\n"
            "  score_against_ground_truth.py --doc demo --extraction-file run.json\n"
            "  score_against_ground_truth.py --doc demo --from-bigquery\n"
            "  score_against_ground_truth.py --doc doc_3 --extraction-file run.json --threshold 0.9\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--doc", required=True, metavar="DOC_ID",
        help="Doc identifier (e.g. demo, doc_3, doc2_25). Used to find ground_truth/<doc>.json.",
    )
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument(
        "--extraction-file", metavar="PATH",
        help="Path to a local extraction JSON file. Accepts either the full envelope "
             "({genomic_pathology_extraction: {...}}) or the inner dict.",
    )
    src.add_argument(
        "--from-local", action="store_true",
        help="Fetch the most-recent extraction for this doc_id from "
             "`local_runs/extractions_v1.jsonl` (default when PERSISTENCE_BACKEND=local).",
    )
    src.add_argument(
        "--from-bigquery", action="store_true",
        help="Fetch the most-recent extraction for this doc_id from BigQuery "
             "(requires GCP auth + extractions_v1 table populated by the pipeline).",
    )
    p.add_argument(
        "--all-sections", action="store_true",
        help="Score ALL sections with ground truth (Phase 2a: variants, panel, "
             "biomarkers, clinical_information) — not just report_metadata. Uses "
             "--extraction-file as the source (a process_local --json dump works).",
    )
    p.add_argument(
        "--threshold", type=float, default=0.80,
        help="Acceptance threshold for pass rate (default: 0.80). "
             "Exits 0 if pass_rate ≥ threshold, else 1.",
    )
    p.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Emit machine-readable JSON report instead of the formatted table.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show notes for every comparison (default: only failed comparisons).",
    )
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-5s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    # ----- Phase 2a: score ALL sections -----
    if args.all_sections:
        try:
            gt_env = load_ground_truth_envelope(args.doc)
            if args.extraction_file:
                ex_env = load_extraction_envelope_file(args.extraction_file)
            else:
                print("ERROR: --all-sections requires --extraction-file "
                      "(e.g. the demo_v2.json from process_local --json).", file=sys.stderr)
                return 2
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR loading for --all-sections: {exc}", file=sys.stderr)
            return 2
        scores = score_all_sections(args.doc, gt_env, ex_env)
        if args.json_output:
            print(json.dumps({"doc_id": args.doc, "sections": [s.to_dict() for s in scores]},
                             indent=2, default=str))
        else:
            _print_section_scores(args.doc, scores)
        agg = (sum(s.f1 for s in scores) / len(scores)) if scores else 0.0
        ok = agg >= args.threshold
        if not args.json_output:
            verdict = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
            print(f"\n  Aggregate mean-F1: {agg:.3f}   Threshold: {args.threshold:.2f}   "
                  f"Verdict: {verdict}\n", file=sys.stderr)
        return 0 if ok else 1

    # ----- Load ground truth (report_metadata-only, Phase 1 path) -----
    try:
        gt = load_ground_truth(args.doc)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR loading ground truth: {exc}", file=sys.stderr)
        return 2

    # ----- Load extraction -----
    # Resolution order:
    #   1. --extraction-file PATH    → read that file
    #   2. --from-bigquery           → BigQuery query
    #   3. --from-local              → local jsonl
    #   4. (no flag)                 → auto-detect: PERSISTENCE_BACKEND env var
    import os
    try:
        if args.extraction_file:
            actual = load_extraction_file(args.extraction_file)
        elif args.from_bigquery:
            actual = await load_extraction_from_bigquery(args.doc)
        elif args.from_local:
            actual = load_extraction_from_local(args.doc)
        else:
            backend = (os.environ.get("PERSISTENCE_BACKEND") or "local").lower()
            if backend == "bigquery":
                actual = await load_extraction_from_bigquery(args.doc)
            else:
                actual = load_extraction_from_local(args.doc)
    except Exception as exc:
        print(f"ERROR loading extraction: {exc}", file=sys.stderr)
        return 2

    # ----- Score -----
    report = score_report_metadata(doc_id=args.doc, expected=gt, actual=actual)

    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        _print_table(report, verbose=args.verbose)

    threshold_ok = report.pass_rate >= args.threshold
    if not args.json_output:
        verdict = (
            f"{GREEN}PASS{RESET}" if threshold_ok else f"{RED}FAIL{RESET}"
        )
        print(
            f"\n  Threshold: {args.threshold:.2f}   Verdict: {verdict}\n",
            file=sys.stderr,
        )
    return 0 if threshold_ok else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv or sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
