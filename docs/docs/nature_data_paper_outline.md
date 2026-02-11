#!/usr/bin/env markdown
# Nature Scientific Data Paper Outline

## Title (Working)

Nationwide Air Permit Extraction Dataset: Structured Emission Unit Records From Public Permits

## Abstract (Draft Points)

- Motivates need for structured permit data across US states.
- Describes pipeline for PDF acquisition, OCR, and LLM extraction.
- Summarizes dataset scope, coverage, and validation.
- Notes roadmap to full US coverage.

## 1. Background And Summary

- Public permit data is fragmented across agencies.
- Dataset supports environmental compliance research and facility analysis.
- Highlights current coverage and planned expansion.

## 2. Methods

### 2.1 Data Collection

- Sources: EPA and state portals; list program types.
- Download scripts and reproducibility notes.

### 2.2 Text Extraction

- OCR and PDF parsing strategy.
- Storage in `data/interim/extracted_text/`.

### 2.3 Information Extraction

- LLM prompt structure and schema.
- Row-level output per emission unit.

### 2.4 Post-Processing

- Completed/failed handling.
- Consolidation into a single tabular output.

## 3. Data Records

- File format(s) and location.
- Schema overview (link to data dictionary).
- Description of columns and missingness conventions.

## 4. Technical Validation

- Manual audit sampling and metrics.
- Automated QC checks and reporting.

## 5. Usage Notes

- Recommended filters (e.g., `Status == Success`).
- Known limitations and coverage gaps.
- Guidance for merging to external datasets.

## 6. Code Availability

- GitHub repository link.
- Release tag or commit for reproducibility.

## Figures And Tables

- Pipeline diagram (data flow from PDFs to structured records).
- Coverage map by state/program.
- Validation summary table (precision/recall by field group).
