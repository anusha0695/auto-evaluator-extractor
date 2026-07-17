"""
Deterministic field-ID generator per FR-1 in sme_capture_requirements.md.

Walks an `extraction_v2.json` envelope and produces a list of stable
`FieldIdentifier` objects — one per reviewable field. Downstream:
  * The UI uses IDs to attach corrections to specific fields.
  * The review_store maps IDs back to values via `path_in_extraction`.
  * On resubmit, IDs let us restore prior actions even after array
    ordering may have shifted (via `natural_key` fallback per FR-1.4).

Section shape observed in the current pipeline (v4):
  extraction_v2.json = {
    report_metadata: { <singular fields> },
    Genomic_Variant_umbrella: {
        count_of_Genomic_Variants: N,
        llm_confidence_score: F,
        Genomic_Variants: [ { <record fields> }, ... ]
    },
    other_molecular_biomarker_umbrella: {
        count_of_other_molecular_biomarkers: N,
        llm_confidence_score: F,
        other_molecular_biomarkers: [ { <record fields> }, ... ]
    },
    tested_biomarker_umbrella: {
        count_of_tested_biomarkers: N,
        page_numbers: [...],
        llm_confidence_score: F,
        tested_biomarkers: [ { <record fields> }, ... ]
    },
    count_of_extracted_objects: int,
  }

Rules:
  * `report_metadata` — every scalar field is reviewable.
  * `<X>_umbrella` — iterate the inner list of records; every scalar
    field of every record is reviewable.
  * Skip keys everywhere: `provenance`, `llm_confidence_score`, any key
    starting with `count_of_`, and `page_numbers`.
  * Skip top-level scalars (e.g. `count_of_extracted_objects`).
  * Skip list-valued fields inside records (not reviewable in v1).
  * Include dict-valued nested subobjects with dotted paths (future-proof).

ID format examples:
  report_metadata.Patient_MRN
  Genomic_Variants[3].amino_acid_change
  Genomic_Variants[3].variant_details.hgvs_c   (if such nesting appears)

Natural keys (FR-1.4):
  v1 — the current pipeline schema has no stable record-level natural key
  (variants have `gene_studied` + other fields but no unique record ID).
  We leave `natural_key=None` for now. Content-hash-based IDs are parked
  as T4 in memory/sme_capture_parked_todos.md.

Design constraints:
  * Pure stdlib (NFR-1.2).
  * Deterministic: same input dict → identical output list on every call.
  * No I/O.
"""
from __future__ import annotations

from typing import Any, Union

from schemas import FieldIdentifier


# ── Skip rules ─────────────────────────────────────────────────────────────
# Never reviewable — these are metadata about the extraction, not extracted data.
_SKIP_KEYS: frozenset[str] = frozenset({
    "provenance",
    "llm_confidence_score",
    "page_numbers",  # umbrella-level list of pages — not a per-field correction target
})
_SKIP_KEY_PREFIXES: tuple[str, ...] = ("count_of_",)


def _should_skip(key: str) -> bool:
    return key in _SKIP_KEYS or key.startswith(_SKIP_KEY_PREFIXES)


def generate_field_ids(extraction: dict[str, Any]) -> list[FieldIdentifier]:
    """Public entry point. See module docstring for the contract."""
    ids: list[FieldIdentifier] = []
    if not isinstance(extraction, dict):
        return ids

    # Iterate top-level in the natural insertion order of the source dict.
    # Python 3.7+ dicts are insertion-ordered; extraction_v2.json is written
    # deterministically by the pipeline, so this preserves stable ordering
    # without needing a manual sort.
    for section_name, section_val in extraction.items():
        if _should_skip(section_name):
            continue
        if not isinstance(section_val, dict):
            # Top-level scalars like `count_of_extracted_objects` land here — skip.
            continue

        if section_name.endswith("_umbrella"):
            _walk_umbrella(section_name, section_val, ids)
        else:
            # e.g. `report_metadata` — singular container of scalar fields
            _walk_singular_section(section_name, section_val, ids)

    return ids


# ── Internals ──────────────────────────────────────────────────────────────

def _walk_singular_section(
    section_name: str,
    section_val: dict[str, Any],
    ids: list[FieldIdentifier],
) -> None:
    """Emit one FieldIdentifier per scalar field in a singular (non-umbrella) section."""
    for k, v in section_val.items():
        if _should_skip(k):
            continue
        path: list[Union[str, int]] = [section_name, k]
        if isinstance(v, dict):
            _walk_nested_dict(f"{section_name}.{k}", v, path, ids, natural_key=None)
        elif isinstance(v, list):
            # Singular sections usually don't hold record lists; skip for v1.
            continue
        else:
            ids.append(FieldIdentifier(
                id=f"{section_name}.{k}",
                natural_key=None,
                path_in_extraction=path,
            ))


def _walk_umbrella(
    umbrella_name: str,
    umbrella_val: dict[str, Any],
    ids: list[FieldIdentifier],
) -> None:
    """
    Descend into an umbrella dict. Locate the inner list-of-records and emit
    one FieldIdentifier per scalar field of every record.

    The ID uses the INNER-LIST name (e.g. `Genomic_Variants`), not the
    umbrella name — matches the shape SMEs see in the UI and keeps IDs
    concise. path_in_extraction retains the full walk (umbrella + list + idx).
    """
    for list_key, list_val in umbrella_val.items():
        if _should_skip(list_key):
            continue
        if not isinstance(list_val, list):
            continue

        for idx, record in enumerate(list_val):
            if isinstance(record, dict):
                record_prefix = f"{list_key}[{idx}]"
                record_path: list[Union[str, int]] = [umbrella_name, list_key, idx]
                _walk_record(record_prefix, record, record_path, ids)
            elif isinstance(record, (str, int, float, bool)) or record is None:
                # Scalar record — the whole array element IS the reviewable value.
                # Real example: tested_biomarker_umbrella.tested_biomarkers = ["JAK2"]
                # ID has no trailing .field; correct/reject_record act on the scalar.
                ids.append(FieldIdentifier(
                    id=f"{list_key}[{idx}]",
                    natural_key=None,
                    path_in_extraction=[umbrella_name, list_key, idx],
                ))
            # else: nested list inside an umbrella — skip (not a v1 pattern)


def _walk_record(
    record_prefix: str,
    record: dict[str, Any],
    record_path: list[Union[str, int]],
    ids: list[FieldIdentifier],
) -> None:
    """Emit one FieldIdentifier per scalar field in a record."""
    for k, v in record.items():
        if _should_skip(k):
            continue
        field_path = list(record_path) + [k]
        if isinstance(v, dict):
            _walk_nested_dict(f"{record_prefix}.{k}", v, field_path, ids, natural_key=None)
        elif isinstance(v, list):
            # Record-level list fields aren't reviewable field-by-field in v1.
            continue
        else:
            ids.append(FieldIdentifier(
                id=f"{record_prefix}.{k}",
                natural_key=None,
                path_in_extraction=field_path,
            ))


def _walk_nested_dict(
    prefix: str,
    subobj: dict[str, Any],
    path: list[Union[str, int]],
    ids: list[FieldIdentifier],
    natural_key: dict[str, Any] | None,
) -> None:
    """Recurse into a nested dict-valued field with dotted ID components."""
    for k, v in subobj.items():
        if _should_skip(k):
            continue
        field_path = list(path) + [k]
        if isinstance(v, dict):
            _walk_nested_dict(f"{prefix}.{k}", v, field_path, ids, natural_key=natural_key)
        elif isinstance(v, list):
            continue
        else:
            ids.append(FieldIdentifier(
                id=f"{prefix}.{k}",
                natural_key=natural_key,
                path_in_extraction=field_path,
            ))
