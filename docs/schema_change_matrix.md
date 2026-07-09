# Schema Change Matrix — what to touch for every operation

When you change the production-contract schema (`config/schemas/genomic_pathology_v4.json`),
this table tells you which config files you also need to look at. Three operations
(**add / delete / update**) × three scopes (**whole schema / section / field**).

The schema is the single source of truth — most other files read from it at runtime
and update automatically. The few config files listed below carry rules that *name*
fields or sections by hand (block-role → field maps, normalization targets,
attribution anchors, etc.), so they need to be checked when the things they name
change.

## Legend

- ✓ = must edit
- ⚠️ = check and maybe edit (depends on whether the rule already references the
  thing you're changing)
- — = no edit needed (the schema or runtime code handles it automatically)

## The matrix

| Scope · Operation | Schema | `production_mapping.yaml` | `teams_v4.yaml` + team prompt overlay | `section_layout.yaml` | `link_registry_v4.yaml` | `dedup_policy.yaml` | `ner_mapping_v4.yaml` | `recall_floor.yaml` | `attribution_map.yaml` | `normalizer_map.yaml` |
|---|---|---|---|---|---|---|---|---|---|---|
| **Whole schema · ADD** (introduce v5 alongside v4) | ✓ create new file | ✓ point `schema:` at new file | ✓ new teams / prompts | ✓ add new section rows | ✓ add new link types | ⚠️ if new dedup rules apply | ✓ new label routing | ⚠️ if new block roles | ⚠️ for new nested attrs | ⚠️ for new normalizable fields |
| **Whole schema · DELETE** (retire v3 entirely) | ⚠️ delete file (optional) | — (v4 already pointed at v4) | ⚠️ delete v3 teams + prompts | ⚠️ remove v3-only rows | ⚠️ remove v3 link rows | ⚠️ remove v3 rules | ⚠️ remove v3 routing | ⚠️ remove v3 rows | ⚠️ remove v3 rows | ⚠️ remove v3 rules |
| **Whole schema · UPDATE** (swap to a renamed v4.1 file) | ⚠️ rename / edit in place | ✓ if filename changed → update `schema:` | — | — | — | — | — | — | — | — |
| **Section · ADD** (e.g. `pharmacogenomics_findings`) | ✓ add top-level property | — (auto-derived from schema) | ✓ new team + prompt | ✓ add row (`record_array`, `empty`, `gene_key_field`) | ⚠️ add link types involving the section | ⚠️ if dedup rule applies | ✓ route NER labels here | ⚠️ if block roles map to it | ⚠️ if section has nested attrs | ⚠️ if normalizable fields |
| **Section · DELETE** (drop one entirely) | ✓ remove top-level property | ⚠️ if you keep schema field but want it suppressed → add to `exclude_sections` instead | ✓ remove team OR set `enabled: false` | ✓ remove row | ✓ remove link rows touching it | ✓ remove dedup rules touching it | ✓ remove routing entries | ⚠️ remove rules | ⚠️ remove rules | ⚠️ remove rules |
| **Section · UPDATE** (rename, reshape) | ✓ rename / restructure | ⚠️ if name appears in `exclude_sections` | ✓ update `schema_section` ref + prompt overlay | ✓ update `record_array` / `empty` | ✓ update endpoint section names | ✓ update section refs | ✓ update routing keys | ⚠️ update section refs | ⚠️ update section refs | ⚠️ update section refs |
| **Field · ADD** (e.g. `tumor_mutational_burden` to a section) | ✓ add to section's `properties` (+ `required` if mandatory) | — | ⚠️ team prompt only if non-trivial domain rules (concatenation, derivation, special boundaries). Field contract auto-renders. | — | — | — | ⚠️ only if a NER label routes specifically to it | ⚠️ only if a block-role maps to it | ⚠️ only if it's a tracked nested attribute | ⚠️ only if it has a canonical form (gene / HGVS / method / biomarker) |
| **Field · DELETE** (e.g. `transcript_reference_sequence`) | ✓ remove from `properties` (and `required` if listed) | — | ⚠️ remove any explicit references in team prompt domain rules | ⚠️ only if it's the section's `gene_key_field` | — | — | ⚠️ only if NER routed here specifically | ⚠️ remove row if present | ⚠️ remove row if present | ⚠️ remove rule if present |
| **Field · UPDATE** (rename or change type) | ✓ rename / retype | — | ⚠️ update prompt domain rules that reference the old name | ⚠️ if it's the `gene_key_field` | — | — | ⚠️ if it has a `target_field_hint` | ⚠️ if `field:` references it | ⚠️ if attribute key references it | ⚠️ if rule references the field |

## How to use this in practice

Before any change, run these two greps — they tell you exactly which `⚠️` cells
actually apply on your codebase:

```bash
# 1. Field/section name references in configs (outside the schema itself)
grep -rn "<field_or_section_name>" config/ | grep -v schemas/

# 2. References in team prompts
grep -rn "<field_or_section_name>" config/prompts/
```

Whatever comes back is your real edit list. Empty output for both → schema edit
alone is sufficient.

## The big-picture rules behind the matrix

1. **Schema is the contract.** Almost every `⚠️` is "rule that happens to name
   this field/section by hand." The schema itself never appears in the `⚠️`
   column for the same change — it's always `✓` or `—` at the top of the row.

2. **`production_mapping.yaml` is policy, not contract.** It never needs to
   track field-level changes. It tracks: which schema file, which sections are
   off, which fields do we withhold — three policy questions.

3. **Team prompts are mostly auto-rendered from the schema** (via
   `core/schema_loader` → `core/prompt_renderer`). The overlay `.j2` files only
   need editing when a field has hand-written domain rules (e.g. the variant
   team's `coding_dna_change` rule says "include the `c.` prefix" — that's
   domain logic the schema can't express).

4. **Verifier configs (`recall_floor`, `attribution`, `normalizer_map`)
   reference fields by name** because they encode pipeline behavior the schema
   can't (block-role mapping, owner keys, normalizable subsets). Those are the
   `⚠️` cells you actually have to check.

## Quick reference — concrete examples

### Removing an ordinary field — `transcript_reference_sequence`

```bash
grep -rn "transcript_reference_sequence" config/ | grep -v schemas/
# → empty
grep -rn "transcript_reference_sequence" config/prompts/
# → empty
```

Edit list: **just the schema**. Done.

### Removing a normalized field — `method`

```bash
grep -rn "method" config/ | grep -v schemas/
# → config/normalizer_map.yaml has a method normalizer rule
# → possibly attribution_map.yaml / section_layout.yaml
```

Edit list: schema + normalizer rule + whatever else came back from the grep.

### Adding a whole section — `pharmacogenomics_findings`

Follow the `Section · ADD` row top to bottom: schema → teams + prompt →
section_layout → link_registry (if relationships) → ner_mapping (so entities
route here) → recall_floor / attribution / normalizer_map (if applicable).

### Swapping to a v5 schema in production output only

`production_mapping.yaml`'s `schema:` line — that's it. No code edit needed for
production. (The extraction side reads from `pipeline/runner.py` for which
schema file each pipeline version uses; that's a separate concern and is
intentionally not part of this matrix since this matrix focuses on
config-driven changes.)
