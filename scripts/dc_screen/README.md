# Data-center self-generation coverage screen (2026-09-02)

Screens the union (`permit_data_union_v5d.csv`), the full-text corpus
(`data/interim/extracted_text` + `data/interim/dc_generator_text`) and the LBL
data-center map for data centers with on-site generation that are missing from
`data/processed/analysis/dc_generation_sites.csv`. Outputs land in
`data/processed/analysis/dc_screen/`; intermediates in `.../dc_screen/work/`.

Run from the repo root, in this order (all paths are repo-relative; `grep` here
is ugrep-compatible but plain GNU grep works too):

1. `python scripts/dc_screen/build_work_table.py`  – slim v5d parquet (30 columns).
2. `python scripts/dc_screen/doc_table.py`          – one row per document: generator signature, NAICS/name flags.
3. `python scripts/dc_screen/site_match.py`         – match documents to site-table sites (doc / address / permit / city+name).
4. `python scripts/dc_screen/lbl_join2.py`          – join LBL DC map facilities to union documents (address, city+operator, zip+operator).
5. `bash scripts/dc_screen/grep_screen.sh`          – full-text screen (DC terms, BTM language, NAICS, operators), ~2 min on 12 GB.
6. `bash scripts/dc_screen/grep_curated.sh`         – known-project / operator name pass (case-insensitive).
7. `python scripts/dc_screen/agg2.py`               – aggregate grep hits per document (strict term classes), join to union.
8. `bash scripts/dc_screen/snips.sh`                – context snippets for candidate documents.
9. `python scripts/dc_screen/verify.py`             – project-tied vs boilerplate context, lane audit, corpus-not-in-union, known-project recall.
10. `python scripts/dc_screen/xlsx_scan.py`         – which "success"-logged documents still have rows in a run xlsx.
11. `python scripts/dc_screen/consolidate.py`       – writes the deliverable CSVs.

`text_screen.py` is kept only as the pattern source for `agg2.py` (the Python
multiprocessing pass itself is far slower than the grep pass and is not used).
Notes: `verify.py` expects `curated_ci.pkl`; the known-project recall block in
`consolidate.py` rebuilds it from `grep_curated.txt` if rerun after step 9.

## Remediation scripts (added 2026-09-02, after the screen)

- `run_recovery_batch.sh LANE.csv` — Vertex Flash batch driver: build inputs (set `TEXT_DIR=` to read
  text from another directory), submit, poll, merge outputs, finalize rows CSV. Logs to stdout.
- `build_new_text_lane.py` — lane CSV of text files (by prefix) not yet in a union.
- `text_extract_new_pdfs.py PDF_DIR TEXT_DIR --prefix xx_` — pdftotext with OCR fallback for new lane PDFs.
- `assemble_v5e.sh` — v5d + recovery rows (replacing the 9 ERROR-field VA permits via
  `append_new_files_to_union.py --replace-files`) + lane-2 rows → `permit_data_union_v5e.csv`.
- `../build_dc_generation_sites.py` — the checked-in site-table builder (see its docstring); run it on the
  new union with `--prior-sites` pointing at the previous table so lat/lon carry forward.
