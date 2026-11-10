# v5a release audit protocol (evidence-based LLM audit, 2026-08-14)

Sample: the same stratified n=200 permit sample as the v5a model panel
(`v5a_sample_manifest.csv`, seed 20260802), so results are directly comparable
with the panel's agreement numbers. For each permit the auditor receives
`inputs/<filename>.json` (the released extraction reconstructed from union
rows) and the extracted source text, and verifies the release against the
document itself — not against another model's opinion.

## Field verdicts

For each of the 10 general fields (Facility Name, Facility Address, Facility
City, Facility State Abbreviation, Facility Zip Code, Permit Number, Issuance
Date, Expiration Date, Regulatory Authority, NAICS Code), exactly one verdict:

- `CORRECT` — the released value is supported by the document (formatting
  differences, case, abbreviations, and equivalent date formats count as
  correct; an address matching street+number with unit dropped counts as
  correct).
- `INCORRECT` — the document clearly states a different value. Requires
  `evidence` (short quote) and `doc_value`.
- `UNSUPPORTED` — released value is non-null but nothing in the document
  supports it (possible hallucination or drawn from outside the doc).
- `MISSED` — released value is null but the document states one. Requires
  `evidence` and `doc_value`.
- `NULL_OK` — released value is null and the document genuinely does not
  contain the field.

## Unit audit

- `released_unit_count`: length of the released Emission Units list.
- Verify up to 6 released units (spread across the list, not just the first):
  does the unit exist in the document, and do its type and capacity (when the
  release states one) match? Verdict per checked unit: `VERIFIED`,
  `WRONG_ATTRS` (exists but type/capacity conflict — quote), `NOT_FOUND`.
- Recall probe: list up to 5 emission units clearly present in the document
  but absent from the released list (ID or description). `recall_complete` =
  true if a reasonable scan found no missing units.
- For documents >200 KB, verification is search-driven (grep for unit IDs,
  capacities, the facility name) rather than a full read; recall probes there
  target the document's emission-unit inventory sections.

## Output

One JSON line per permit in `results/<batch_id>.jsonl`:

```json
{"filename": "...", "fields": {"Facility Name": {"verdict": "CORRECT"}, ...,
  "Permit Number": {"verdict": "INCORRECT", "doc_value": "...", "evidence": "..."}},
 "units": {"released_unit_count": 12, "checked": [
    {"unit": "EU-1 boiler", "verdict": "VERIFIED"}, ...],
  "missing_units": ["..."], "recall_complete": false},
 "notes": "one line, only if something notable"}
```

Aggregation: `aggregate_audit.py` → `audit_results_fields.csv`,
`audit_results_units.csv`, `audit_summary.md`.
