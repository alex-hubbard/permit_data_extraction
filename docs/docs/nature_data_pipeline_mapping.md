#!/usr/bin/env markdown
# Pipeline To Paper Mapping (Nature Scientific Data)

This document maps the current extraction pipeline to the Methods and Data Records
sections of a Nature Scientific Data dataset paper. It also clarifies inputs,
outputs, and intermediate artifacts to support reproducibility.

## Pipeline Stages And Paper Sections

| Stage | Purpose | Code Entry Points | Inputs | Outputs | Paper Section |
| --- | --- | --- | --- | --- | --- |
| Permit acquisition | Collect PDFs from EPA and state portals | `download_epa_permits.py`, `permit_data_extraction/epa_pdf_downloader.py`, `permit_data_extraction/state_permit_scraper.py`, `scripts/*` | Public agency portals | `data/raw/epa_final_permits/`, `data/raw/downloaded_pdfs/` | Methods (Data collection) |
| PDF discovery | Enumerate PDFs for processing | `run_extraction_pipeline.py` (`find_all_pdfs`) | `data/raw/` | In-memory list of PDF paths | Methods (Data collection) |
| Text extraction | Extract text with OCR fallback | `permit_data_extraction/ocr.py`, `run_extraction_pipeline.py` (`extract_text_from_all_pdfs`) | PDFs | `data/interim/extracted_text/*.txt` | Methods (Text extraction) |
| LLM extraction | Convert unstructured text into structured fields | `permit_data_extraction/dataset.py` (`process_text_file`) | Text files | Row-wise structured records | Methods (Information extraction) |
| Data records | Persist structured outputs | `permit_data_extraction/dataset.py` (`append_rows_to_excel`) | Extracted rows | `data/processed/permit_data_extracted.xlsx` | Data Records |
| QA/QC | Track processing status and errors | `permit_data_extraction/dataset.py` (completed/failed folders, status fields) | Text files | `data/interim/extracted_text/completed/` and `/failed/` | Technical Validation |

## Data Flow Summary

1. Source PDFs are downloaded into `data/raw/`.
2. The pipeline discovers all PDFs and extracts text into `data/interim/extracted_text/`.
3. Each text file is processed with an LLM prompt and converted into structured rows.
4. Structured rows are appended to `data/processed/permit_data_extracted.xlsx`.

## Traceability And Provenance Hooks

- **Filename** is preserved as a stable linkage key between the PDF, text file, and
  extracted row(s).
- **Processing Date** and **Model Used** are recorded in each output row.
- **Status** captures LLM extraction outcome and allows filtering for QC.

## Recommended Additions For The Paper

- Document known coverage gaps by state/program.
- Include a reproducibility table listing required API keys and software versions.
- Add a flow diagram illustrating the stages above.
