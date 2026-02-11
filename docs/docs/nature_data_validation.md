#!/usr/bin/env markdown
# Technical Validation Plan

This plan defines a manual audit strategy and metrics for evaluating extraction
quality in a Nature Scientific Data submission.

## Manual Audit Design

### Sampling Frame

- **Population:** all processed permits with rows in `data/processed/permit_data_extracted.xlsx`.
- **Stratification axes:** state/program, permit type (Title V/PSD/etc. if known),
  and document length (e.g., short/medium/long by page count).
- **Minimum coverage:** at least 1 permit per state/program in the current release,
  with oversampling in high-volume states to stabilize metrics.

### Sample Size Guidance

- Target **n = 200 permits** for initial release, adjusted based on total coverage.
- For each sampled permit, review **all rows** (all extracted emission units).

### Annotation Protocol

- Human reviewers compare extracted fields against the source PDF.
- Store audit labels in a separate table with:
  - `Filename`
  - `Field Name`
  - `Extracted Value`
  - `Reference Value`
  - `Correct` (boolean)
  - `Notes`

## Metrics

### Field-Level Metrics

- **Precision:** fraction of extracted values that match the reference.
- **Recall:** fraction of reference values that are correctly extracted.
- **Completeness:** fraction of non-null extracted values per field.

### Unit-Level Metrics

- **Unit identification accuracy:** whether emission units are correctly detected.
- **Unit count error:** absolute difference between extracted and reference unit counts.

### Permit-Level Metrics

- **Coverage:** proportion of permits with at least one correctly extracted unit.
- **Failure rate:** fraction of permits with `Status` indicating failure.

## Automated QC Checks (Secondary)

- Schema validation (required columns, types).
- Duplicate detection by `Filename` + unit identifiers.
- Cross-field checks (e.g., `Capacity Value` should have `Capacity Unit`).
- Missingness profiling by state/program.

## Reporting Format

- Summary table of precision/recall by field group (general vs unit fields).
- Histogram of unit counts per permit (extracted vs reference).
- Stratified performance table by state/program.
