# Nature Scientific Data Paper — Outline

**Working title:** Nationwide Air Permit Extraction Dataset: Structured Emission-Unit Records From Public Title V Permits

> Outline follows the *Scientific Data* Data Descriptor format. Each section lists the key points to cover.

---

## Abstract
- The problem: facility-level emissions data locked in heterogeneous permit PDFs across many agencies.
- What Title V permits contain (units, controls, enforceable limits) and why they matter.
- What we built: reproducible pipeline (acquire → extract text → LLM extraction → consolidate).
- What we release: single tabular file, one row per emission unit, linked to source PDF.
- Scope of initial release and roadmap to broader coverage.

## 1. Background & Summary
- **1.1 Data source — Title V permits:** what Title V is (1990 CAA Amendments), major-source thresholds, ~10,000+ permits, why they are high-value but fragmented.
- **1.2 Motivation & scope:** fragmentation across portals; need for structured, comparable facility/unit records; use cases (compliance research, emissions inventories, technology assessment, environmental justice).
- **1.3 Contribution:** structured dataset + open, reproducible pipeline; current coverage and expansion plan.

## 2. Methods
- Overview: four pipeline stages; pointer to repository for low-level detail.
- **2.1 Data collection:** agentic crawler (`pdf_downloader.py`); state-specific scripts (`scripts/`); EPA permit hub downloader; pipeline input enumeration; provenance recording (source URLs, run dates, options).
- **2.2 Text extraction:** native PDF parsing (PyPDF2); OCR fallback (Poppler + Tesseract); one `.txt` per PDF; filename as stable key; failure logging.
- **2.3 Information extraction:** single LLM call per file; prompt + schema (general permit fields + emission-unit array); row construction; status/failure handling.
- **2.4 Post-processing & consolidation:** fixed schema; dedup on `Filename`; output to `permit_data_extracted.xlsx`; all rows retained; reproducibility (commit/tag, PDF manifest, environment).

## 3. Data Records
- Distribution format and location of the tabular file.
- **3.1 Record structure & traceability:** one row per unit (or per permit if none); repeated permit fields; `Filename` linkage key.
- **3.2 Schema overview:** three column groups — provenance, general permit fields, emission-unit fields; pointer to data dictionary (`nature_data_schema.md`).
- **3.3 Missingness, errors & status codes:** null conventions; rows retained for failures; `Status` as authoritative usability flag.

## 4. Technical Validation
- **4.1 Manual audit:** stratified sample (jurisdiction/portal, OCR vs. native, tabular vs. narrative); field-level accuracy by field group.
- **4.2 Automated QC & consistency checks:** provenance-field presence, date format checks, empty/improbable unit IDs, missingness summaries, status-code distribution / yield.
- **4.3 Reporting & known error modes:** metrics table by field group and stratum; OCR artifacts, free-text limits, facility/unit conflation.
- **4.4 External validation — ORNL In-Plant Training (INPLT) round-trip:** independent, field-informed review by DOE/ORNL Technical Account Managers (TAMs) of 177 process-heating unit rows covering 17 plants matched to INPLT engagements.
  - **Two review rounds (2026-06-26 and 2026-07-16), zero corrections to any extracted value in either round.** Round 2 added TAM comments for 8 plants; two plants (3M ×2) and Intertape were explicitly confirmed accurate from reviewer knowledge.
  - **Capacity agreement through unit conversion:** TAM-reported steam outputs / boiler horsepower reconcile with our extracted heat-input ratings (FMC: 45/60 kpph ↔ 54.4/74.5 MMBtu/hr; Sugar Creek Hurst boilers: 500/600 hp ↔ 21/24.8 MMBtu/hr), i.e., independent field knowledge and permit-derived values describe the same equipment on different bases.
  - **Discrepancy taxonomy from the exercise (all three classes verified against source text):**
    1. *Document scope, not extraction error* — equipment TAMs know on site but absent from the permit text (FMC's small fire-tube boiler, Novelis's soaking pits and 3 of 4 pusher furnaces, H&V Plant 2's electric melter/fiberizers). Title V permits do not enumerate all process equipment, especially electric units and insignificant sources → feeds Usage Notes 5.3.
    2. *Permit-vs-field disagreement with faithful extraction* — Sugar Creek's Superior boilers: permit states 14.00 MMBtu/hr (extracted verbatim); TAM recalls 1,300 hp. The dataset reproduces the legal document, which may lag physical reality.
    3. *Extraction recall on very large documents* — the Frito-Lay Frankfort boilers missed by the initial run (present in 433 KB/845 KB permit texts) motivated the forced-chunking re-extraction campaign (§2.3/4.2); the re-extraction also corrected one INPLT row's capacity to the verbatim permit value.
  - Framing: round-trip with domain experts as a validation pattern complementary to the manual audit — it tests both extraction fidelity and the boundary of what permits can be expected to contain.

## 5. Usage Notes
- **5.1 Recommended filtering:** filter to success statuses; retain failures for coverage analysis.
- **5.2 Unit-level interpretation:** non-standardized unit definitions; limits stored as text; downstream normalization needed.
- **5.3 Coverage & limitations:** acquisition-defined coverage; document heterogeneity; crawler limitations (auth, iframes, non-link delivery).
  - **Geographic coverage is repository-driven, not facility-density-driven:** the corpus mirrors which agencies publish permit documents in bulk-accessible online repositories. Benchmarked against EPA ECHO's 13,546 active major air facilities (`scripts/benchmark_coverage_vs_echo.py` → `coverage_vs_echo_majors.csv`): strong coverage where agencies run open document portals (PA, IN, CA districts, FL, IL, most of New England/Northwest); sparse in the central/southern plains and parts of the West and South because those agencies expose permits only via search-per-facility portals, records requests, or not at all — TX ~7% of 2,481 majors, LA 4/475, KS 1/243, OK 13/244, NE 7/122, WI 7/326, AZ 24/127, KY 70/262, NJ 37/227.
  - State with the largest absolute gap is Texas (18% of the national major-source universe); scrapers for TCEQ's Central File Room and CA SCAQMD's OnBase portal exist in `scripts/` as the first expansion targets, with LA DEQ's EDMS the next-highest-value portal.
  - Caveat for users: per-state analyses and national aggregates must treat coverage as non-random with respect to geography (and hence industry mix); include the per-state coverage-ratio table so users can weight or filter.
  - Equipment-scope caveat (from §4.4): permits under-represent electric process equipment and insignificant sources; unit counts are lower bounds both per document (extraction recall) and per facility (document scope).
- **5.4 Linking to external data:** `Filename` as join key; `Permit Number`/facility identifiers with fuzzy matching to external registries.

## 6. Code Availability
- GitHub repository; cited release tag / commit hash; optional archived DOI (e.g., Zenodo).
- Components: acquisition tooling, text-extraction pipeline, LLM extraction + dataset writer; README and environment dependencies.

## Figures & Tables
- **Fig. 1 — Pipeline diagram:** portals → downloaders → PDF corpus → text extraction → LLM extraction → tabular output.
- **Fig. 2 — Coverage map:** geographic coverage by state/portal with counts and success rates; choropleth of coverage ratio vs. ECHO active major facilities (data: `coverage_vs_echo_majors.csv`) to make the repository-driven pattern explicit.
- **Table 1 — Validation summary:** audit metrics by field group, stratified by jurisdiction and OCR use; row for the ORNL external round-trip (177 rows, 2 rounds, 0 value corrections).
- **Table 2 — Schema / data dictionary** (or reference to `nature_data_schema.md`).
