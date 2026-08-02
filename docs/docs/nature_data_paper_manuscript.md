# A Structured Dataset of U.S. Air Permit Records Extracted From Public Title V Documents Using Large Language Models

> **EDITORIAL NOTES (resolve before submission; remove this block).**
> 1. **Which file is the release artifact?** `permit_data_full.csv/.xlsx` has 97,757 rows from 16,434 permits (41 columns); `permit_data_extracted_combined` has 27,652 rows from 1,552 permits (37 columns). The draft's "~30,000 records" matched the combined file but its earlier "42,000 PDFs / 40 states" framing matched the full corpus. This draft now uses the **full dataset** numbers throughout — change them if the combined file is the intended release.
> 2. **Schema mismatch.** The code's current schema has 47 columns, but the released data has 41: `Owner/Operator Name`, `SIC Code`, `Opacity Limit`, `Throughput/Production Limit`, and `Applicable NESHAP/NSPS Subpart` exist only in the code, not in the extracted data. Either re-run extraction with the expanded schema before submission, or keep the trimmed claims now in this draft (Sections 3.2 and 5.4–5.6 were adjusted accordingly).
> 3. **Model heterogeneity.** Records were produced by seven model identifiers (`lbl/llama` ≈65% of rows / 86% of permits, `amazon/gpt-oss-120b` ≈30% of rows / 10% of permits, Gemini 2.0 Flash/Flash-Lite ≈5% of rows, others). Reviewers will probe cross-model consistency. Note `lbl/llama` — the dominant model — is now deprecated upstream and cannot be re-run, so direct cross-model validation of it is impossible; instead it is covered by the manual audit (Section 4.1), whose n = 200 sample is 85% `lbl/llama` permits and audits the actual released records.
> 4. **Technical Validation status (updated 2026-06-15).** Section 4.2 (multi-model cross-validation, two June panels, 202 permits / 194 usable / 1,052 unit-model comparisons, plus an n = 61 March panel as a robustness check) and Section 4.3 (automated QC on the full dataset) contain real, executed results; the permits lost to the API gateway authorization lapse have been recovered via re-validation and folded in. Still outstanding: the manual audit — the stratified n = 200 sample and reviewer workbook are built at `data/processed/validation/manual_audit/`, human review pending. This audit is also the only validation path for `lbl/llama` (deprecated upstream, cannot be re-run); the sample is 85% `lbl/llama` permits. See `data/processed/validation/README.md`.

## Abstract

Air quality permits issued under Title V of the U.S. Clean Air Act contain detailed, facility-level information about emission sources, control equipment, pollutant limits, and regulatory applicability, yet this information remains largely inaccessible for research because it is distributed across thousands of heterogeneous PDF documents hosted on dozens of state and federal agency portals. We present a structured, emission-unit-level dataset extracted from more than 45,000 publicly available air permit documents acquired from EPA and state agency portals spanning 40 U.S. states and the District of Columbia. The current release contains 97,757 records derived from 16,434 unique permit documents, with 41 standardized fields covering facility identification, permit metadata, and equipment-level attributes including unit capacities, fuel types, pollutant-specific emission limits, control devices, rated efficiencies, and operating constraints. Records are produced by a reproducible, open-source pipeline that combines automated permit acquisition from public portals, text extraction with OCR fallback for scanned documents, and large language model (LLM)-based information extraction, with the model used for each record documented in the data. Each record is linked to its source PDF via a stable filename key, enabling independent verification. We describe the data collection methodology, record schema, technical validation approach, and known limitations. This dataset enables facility-level environmental research, emissions technology assessments, regulatory analysis, and environmental justice investigations at a national scale previously impractical with manual permit review.

## 1. Background and Summary

### 1.1 The Title V Operating Permit Program

Title V of the 1990 Clean Air Act Amendments (42 U.S.C. sections 7661--7661f) established a comprehensive operating permit program for major sources of air pollution in the United States. The program requires facilities that emit above specified thresholds---generally 100 tons per year of any criteria pollutant, 10 tons per year of a single hazardous air pollutant (HAP), or 25 tons per year of combined HAPs---to obtain federally enforceable operating permits administered by state, local, tribal, or territorial agencies (collectively, "permitting authorities"), or by the U.S. Environmental Protection Agency (EPA) where no approved state program exists.

Title V permits serve as comprehensive compliance documents: each permit consolidates all applicable federal and state air quality requirements, emission limits, monitoring protocols, recordkeeping obligations, and reporting requirements into a single instrument. Permits are typically renewed on a five-year cycle and contain structured information about the permitted facility and its individual emission sources (emission units), including equipment descriptions, rated capacities, fuel types, pollutant-specific emission limits, opacity and visible emission limits, throughput and production constraints, pollution control equipment, and citations to applicable federal standards such as National Emission Standards for Hazardous Air Pollutants (NESHAP) and New Source Performance Standards (NSPS). There are an estimated 10,000 or more active Title V permits nationwide, making the program one of the most information-rich---yet operationally fragmented---sources of facility-level environmental data in the United States.

### 1.2 The Data Access Problem

Despite their public availability, Title V permits present substantial barriers to systematic analysis. Permits are distributed across more than 50 permitting authority websites, each with distinct portal architectures, document naming conventions, and access mechanisms ranging from static HTML listings to JavaScript-rendered applications. The documents themselves are heterogeneous PDF files---some born-digital with extractable text, others scanned images requiring optical character recognition (OCR)---with no standardized format across jurisdictions. Within a single state, permit structure may vary by region, time period, or permit writer. This fragmentation means that researchers seeking to analyze emission sources, control technologies, or regulatory stringency at a national scale must manually locate, download, read, and transcribe information from hundreds or thousands of individual documents.

Previous efforts to compile facility-level air quality data have relied on self-reported emissions inventories such as the EPA's National Emissions Inventory (NEI) [3] or the Toxics Release Inventory (TRI) [4], or on permit-level registries such as the Enforcement and Compliance History Online (ECHO) database [5]. Sector-specific databases provide equipment-level detail for narrow source categories---the Emissions & Generation Resource Integrated Database (eGRID) [6] and the Clean Air Markets Program Data [7] for electric generating units, for example---and the Facility Registry Service (FRS) [8] provides authoritative facility identifiers across EPA programs. While valuable, these sources typically lack the equipment-level granularity present in permits for the broader universe of industrial sources: specific emission unit capacities, unit-level emission limits, control device associations, fuel types, throughput restrictions, and applicable federal subpart citations. The gap between what is recorded in permits and what is available in structured databases limits research on emissions technology adoption, regulatory effectiveness, control equipment performance, and cumulative facility-level impacts, including environmental justice analyses that require knowing what equipment operates where [9].

Recent work has demonstrated that large language models can extract accurate structured records from heterogeneous scientific and technical documents at scale [10,11], offering a practical alternative to hand-built parsers for document corpora---like air permits---whose formats vary too widely for rule-based extraction. This dataset applies that approach to the Title V permit corpus.

### 1.3 Dataset Overview

We present a structured dataset of emission-unit-level records extracted from publicly available U.S. air permit documents. The dataset is produced by an open-source, reproducible pipeline that automates permit acquisition, text extraction, and information extraction using large language models (LLMs). The current release includes records derived from a corpus of more than 45,000 permit PDFs obtained from EPA and state agency portals across 40 states and the District of Columbia, of which 16,434 unique permit documents have been processed to date, yielding 97,757 structured records with 41 standardized fields.

Each record corresponds to one emission unit identified within a permit (or one permit-level record when no individual units are identified) and contains fields organized into three groups: (1) facility and permit identification (facility name, address, geographic identifiers, industry classification, permit number, issuance and expiration dates, regulatory authority, and primary applicable regulations); (2) emission unit attributes (unit identifier, description, equipment type, manufacturer, model, year of manufacture, capacity, fuel type, pollutants, emission limits, control devices, rated efficiency, annual run hours, and generation capacity); and (3) processing provenance (source filename, extraction status, processing date, and model used). Every record is linked to its source PDF via a stable filename key, enabling independent verification and auditing.

The pipeline and dataset are designed to support expansion to additional jurisdictions and permit programs. All acquisition, extraction, and post-processing code is released in an accompanying open-source repository with documented entry points and environment specifications for full reproducibility.

## 2. Methods

The dataset is produced by a four-stage pipeline: (1) acquisition of permit PDFs from public portals, (2) text extraction from PDFs with OCR fallback, (3) LLM-based information extraction, and (4) post-processing and consolidation into a single tabular output. The accompanying code repository contains the full implementation; we describe each stage at the level needed for reproducibility while referring readers to the repository documentation for low-level implementation details.

### 2.1 Data Collection

Source documents are Title V operating permits in PDF form, obtained from publicly accessible agency portals. Because permit portals vary widely in architecture---from static HTML pages to JavaScript-heavy single-page applications, and from flat file listings to nested document repositories---acquisition is implemented using three complementary approaches.

**Agentic portal crawling.** The repository includes a general-purpose "agentic" downloader that can explore unfamiliar portals starting from seed URLs. The script uses Selenium to render pages, extracts candidate hyperlinks (prioritizing links found in tables, which frequently contain structured permit listings), and identifies files that appear to be permits or PDFs. For ambiguous links, the downloader optionally uses an LLM to score link text and page context for relevance, then follows high-confidence links to a configurable depth within the same domain. Downloads use a resilient strategy that first attempts direct HTTP fetches with retries and session cookies, falling back to browser-driven downloads when direct access is blocked or when links resolve through intermediate landing pages.

**State-specific download scripts.** For portals with consistent structure and high document volume, the repository provides targeted scripts that encode portal-specific logic such as table parsing, pagination handling, and document link normalization. This approach reduces false positives and improves coverage for high-yield jurisdictions. Representative examples include scripts for Virginia DEQ, Connecticut DEEP, Wyoming DEQ, Ohio EPA, Iowa DNR, and additional states.

**EPA permit hub.** The EPA's centralized permit hub is a JavaScript application accessed via Selenium automation. The downloader loads permit pages, locates the "Permitting Authority Documents" table, identifies the "Final Permit" row, and triggers the PDF download. Completed links are tracked to support incremental acquisition.

All downloaded PDFs are written to a structured directory hierarchy under the project's data directory. The extraction pipeline enumerates PDFs across all acquisition sources, excluding files already processed.

### 2.2 Text Extraction

Each PDF is converted to a single plain-text file for downstream LLM processing.

**Native extraction.** Text is first extracted using the PyPDF2 library [12]: each page is read and concatenated with page-break markers preserved. If the resulting text is non-empty and averages at least 30 characters per page, it is used directly.

**OCR fallback.** When native extraction fails, yields no text, or falls below the per-page character threshold, the pipeline falls back to optical character recognition. PDF pages are rendered to images at 200 DPI using Poppler (via pdf2image), and text is extracted with Tesseract [13] in five-page chunks to bound memory use. This dual-path approach accommodates both born-digital and scanned permit documents.

One text file per PDF is written to an intermediate directory. The filename (without extension) serves as the stable key linking the PDF, extracted text, and downstream records. Files that already exist or have been processed are skipped; failed extractions are logged.

### 2.3 Information Extraction

Structured data are extracted from each text file by a single LLM call. The prompt requests a JSON object containing general permit fields and an array of emission unit objects, each with the full set of unit-level fields. The full prompt template is included in the code repository and archived with the dataset. The prompt includes explicit instructions for field formatting (ISO 8601 dates, semicolon-delimited pollutant and limit lists, normalized fuel types), disambiguation guidance (facility address versus corporate address, facility-level operating hours versus unit-level run hours, emission point versus emission unit identifiers), and a complete example JSON object illustrating the expected output structure. The model is instructed to use null for missing values and to extract only information explicitly stated or clearly implied in the permit text.

**Models.** Extraction calls are issued through an OpenAI-compatible institutional API gateway (CBORG, Lawrence Berkeley National Laboratory), which provides access to multiple hosted models under a uniform interface. The dataset was produced over multiple processing campaigns; each record documents the exact model that produced it in the `Model Used` field. Models used include Llama [NOTE: confirm exact version, e.g. Llama 3.x 70B/405B, behind the `lbl/llama` alias] (≈58% of records), gpt-oss-120b [14] (≈27%), and Gemini 2.0 Flash and Flash-Lite [15] (≈4%), with small contributions from other models. [NOTE: decide whether to standardize on a single model in a final re-run; if not, Section 4 should report validation metrics stratified by model.]

**Schema.** General permit fields include: Facility Name, Facility Address, City, State Abbreviation, Zip Code, County, NAICS Code, Operating Hours, Industry Description, Permit Number, Issuance Date, Expiration Date, Regulatory Authority, and Primary Applicable Regulations. Emission unit fields include: Unit ID, Unit Description, Unit Quantity, Unit Make, Unit Model, Year of Manufacture, Unit Type, Pollutants, Emission Limits, Control Device(s), Capacity Value, Capacity Unit, Fuel Type, Rated Efficiency, Annual Run Hours, and Generation Capacity. Post-processed fields (Operating Hours Value, Operating Hours Time Basis, Annual Run Hours Value, Annual Run Hours Time Basis) are derived from the raw text fields using pattern-based parsing after LLM extraction. [NOTE: if the corpus is re-extracted with the expanded 47-field schema, add Owner/Operator Name, SIC Code, Opacity Limit, Throughput/Production Limit, and Applicable NESHAP/NSPS Subpart here and restore the corresponding Usage Notes.]

**Row construction.** General fields are copied to every row for the same permit. Each emission unit in the JSON array becomes one row with unit-level fields populated. If no emission units are identified, a single row is emitted with unit fields null. Each row is tagged with the source filename, processing status, processing date, and model identifier.

**Failure handling.** When extraction fails (malformed JSON, API errors, or text reading failures), the corresponding row is retained with an appropriate status code and sentinel values, ensuring that all processed files are represented in the output and failure rates are transparent.

**Large document handling.** Documents that exceed the model's context window are automatically split into chunks of up to 320,000 characters, each chunk is extracted independently, and the results are merged; if chunked extraction still exceeds the context limit, the pipeline retries with a configurable larger-context model. This enables extraction from lengthy permits without manual intervention.

### 2.4 Post-Processing and Consolidation

All extracted rows are consolidated into a single tabular file. Post-processing steps include: (1) parsing of free-text operating hours and annual run hours fields into separate numeric value and time basis columns; (2) normalization of fuel type strings to canonical forms (e.g., "nat gas" to "natural gas," "#2 oil" to "#2 fuel oil"); (3) normalization of ZIP codes to five- or nine-digit form and dates to ISO 8601; (4) inference of capacity values from unit descriptions via pattern matching (MMBtu/hr, MW, hp, cfm, tons/day, lb/hr) when the structured capacity field is empty; and (5) facility-level deduplication: when multiple permit documents describe the same facility (matched on normalized facility name, address, city, state, and ZIP code), the most recently processed extraction is retained, the number of contributing source documents is recorded in the `Duplicate Equipment Documents` field, and the retained source document is recorded in the `Latest Facility Filename` field. The consolidated output is written as an Excel file with columns ordered for analyst convenience: processing provenance, facility identification, emission unit attributes with parsed subfields, permit identifiers, and file metadata.

**Reproducibility.** A specific dataset version can be reproduced given the repository commit or release tag, the set of source PDFs (or a documented manifest), and the runtime environment (Python packages, OCR tooling, and LLM API configuration). The repository README specifies exact commands, paths, and environment requirements.

## 3. Data Records

The primary dataset is distributed as a single tabular file in both CSV and Excel formats, archived with a persistent identifier [DOI placeholder: Zenodo deposit, to be minted at submission]. Per-state subsets and a manifest are provided for convenience.

### 3.1 Record Structure

Each record corresponds to one emission unit extracted from a permit, or a single permit-level record when no individual units are identified. Permit-level fields are repeated across all unit rows for the same permit, making the dataset self-contained for filtering and aggregation without requiring joins. The `Filename` column serves as the primary linkage key to the source PDF and intermediate text file.

### 3.2 Schema Overview

The dataset contains 41 columns organized into four groups:

**Processing and provenance fields** (4 columns): `Status`, `Processing Date`, `Model Used`, and `Filename`. These record which source document produced a row, whether extraction succeeded, and what model produced the record. Status values include "Success," "Success (No Units Found)," "LLM Extraction Failed," and "Text Reading Failed."

**General permit fields** (16 columns): Facility identification (Facility Name, Facility Address, Facility City, Facility State Abbreviation, Facility Zip Code, Facility County), industry classification (NAICS Code), operational context (Operating Hours, Operating Hours Value, Operating Hours Time Basis, Industry Description), and permit identification (Permit Number, Issuance Date, Expiration Date, Regulatory Authority, Primary Applicable Regulations).

**Emission unit fields** (18 columns): Unit identification (Unit ID, Unit Description, Unit Quantity, Unit Type), equipment attributes (Unit Make, Unit Model, Year of Manufacture, Capacity Value, Capacity Unit, Fuel Type, Rated Efficiency, Generation Capacity), operational parameters (Annual Run Hours, Annual Run Hours Value, Annual Run Hours Time Basis), and regulatory content (Pollutants, Emission Limits, Control Device(s)).

**File metadata fields** (3 columns): Spec Sheet Link, Duplicate Equipment Documents, and Latest Facility Filename, supporting deduplication and cross-referencing.

### 3.3 Coverage

The current release includes records from permits obtained across 40 U.S. states and the District of Columbia: 97,757 records derived from 16,434 unique permit documents, drawn from an acquired corpus of more than 45,000 source PDFs. Of these records, 92,235 (94.4%) carry status "Success," 2,302 (2.4%) "Success (No Units Found)," 3,216 (3.3%) "LLM Extraction Failed," and 4 "Text Reading Failed." Coverage reflects the permits that were publicly accessible and successfully processed at the time of each acquisition run; it does not represent a census of all Title V permits in any jurisdiction. States with the largest record counts are Pennsylvania (21,706 successful records), California (13,254), Michigan (6,943), Florida (5,064), and Indiana (5,006). [NOTE: reconcile state-field anomalies before release---a small number of records carry malformed state values (e.g., "A", "CA, WA", "ERROR", "QC") that should be cleaned or documented.]

### 3.4 Representativeness against external benchmarks

Because acquisition targets publicly accessible permit repositories rather than a defined statistical frame, we characterize the dataset's representativeness against two external references: the EPA ECHO universe of active major (Title V) air facilities, which defines the target population, and U.S. Census economic surveys, which weight that population by industrial activity. Dataset facilities were matched to the ECHO facility universe within each state by normalized facility name (exact match, then fuzzy matching at a conservative token-set threshold); coverage is the fraction of ECHO major facilities matched by at least one dataset facility. Nationally, the dataset covers 63.2% of the 13,546 active major air facilities in ECHO. Coverage remains uneven across states but is no longer bimodal: 22 states covering 6,269 majors are well covered (at least 70%), 20 states covering 5,963 majors are partially covered (30--70%), and 10 states covering 1,314 majors remain sparse (below 30%). The residual sparse tier consists almost entirely of jurisdictions that expose permits only through per-facility search portals, bot-protected applications, or records requests rather than bulk-accessible repositories; Kansas (243 majors) and Nebraska (122) remain the only states with no coverage at all.

To test whether these gaps track economic importance or merely repository availability, we weighted state-level coverage by state manufacturing activity from the Census Annual Survey of Manufactures (ASM, 2021) [19] and the County Business Patterns (CBP, 2022) [20]. ASM reports value added, shipments, and employment for the manufacturing sector (NAICS 31--33) by state, providing an economic-magnitude measure that establishment counts alone do not. Weighting each state's coverage by its manufacturing value added yields a national coverage of 66.0%, and weighting by manufacturing employment yields 65.6%, both above the unweighted mean of state coverages (57.0%): the dataset covers economically larger manufacturing states better than smaller ones (Spearman +0.37 for value added, +0.31 for employment). At the county level, CBP establishment counts indicate near-independence between coverage and local manufacturing intensity: coverage is 59.5%, 61.8%, 64.0%, and 64.4% from the least to the most manufacturing-intensive quartile, with a negligible rank correlation (Spearman 0.02--0.04). Coverage is likewise even across industrial sectors. Representation ratios---each sector's share of matched facilities divided by its share of the ECHO universe---fall between 0.87 and 1.09 for every sector accounting for more than 5% of the universe, including manufacturing (1.06--1.09), utilities (0.98), mining and oil and gas (0.87), and transportation (0.97). Together these indicate that residual coverage gaps are driven predominantly by the geographic distribution of accessible repositories rather than by systematic under- or over-representation of economically significant or sectorally distinct activity.

Targeted acquisition demonstrably closes these gaps. The states identified as the largest economic blind spots in an earlier snapshot of this dataset have since been ingested from newly developed repository interfaces and now rank among the better-covered jurisdictions: Louisiana moved from no coverage to 94% of its 475 majors, Wisconsin from near-zero to 78%, Texas---the second-largest manufacturing state at 9.8% of national manufacturing value added---from 6% to 66%, and Ohio from 38% to 74%. The mining and oil and gas sector, previously the most under-represented sector at a representation ratio of 0.50, rose to 0.87 as a direct consequence of the Texas and Louisiana ingestions.

The largest remaining gaps are correspondingly smaller in economic magnitude and concentrated in jurisdictions whose permits are not retrievable in bulk: Minnesota (40% covered, 2.2% of national manufacturing value added), New Jersey (9%, 1.8%), Oregon (29%, 1.3%), Kansas (0%, 1.3%), and Arizona (9%, 1.3%). These reflect specific access barriers rather than acquisition priority---bot-protected portals, account-gated document systems, permits distributed across county-level programs, and agencies that publish no final permits online. The representativeness statistics reported here reflect coverage at a fixed point in the acquisition timeline and are expected to improve as further repositories are ingested. The benchmark is fully reproducible from the ECHO, ASM, and CBP sources via a script included in the repository.

[NOTE: the coverage, NAICS, ASM, and CBP figures in this subsection were computed on the consolidated union dataset as of 2026-08-01 (`permit_data_union_v5a.csv`, 56,749 permits / 1,109,121 records) rather than the release snapshot described in Section 3.3 (16,434 permits); Section 3.3 MUST be regenerated from the same snapshot before submission---the two sections currently describe different datasets by an order of magnitude. Supplementary per-state, per-sector, and per-county tables (`coverage_vs_echo_majors.csv`, `coverage_by_naics.csv`, `asm_representativeness_state.csv`, `cbp_representativeness_county.csv`) can be promoted to numbered tables.]

### 3.5 Missingness and Status Conventions

Missing values are represented as null or empty cells. When extraction fails for a permit, the dataset retains a row with the corresponding status code and null or sentinel values in content fields. This design allows users to quantify coverage, failure rates, and extraction quality across the dataset. Users should filter on the `Status` column as described in Usage Notes.

## 4. Technical Validation

Technical validation quantifies extraction quality and characterizes error modes arising from document heterogeneity, OCR artifacts, and LLM interpretation of ambiguous permit language.

### 4.1 Manual Audit

We manually audited a stratified random sample of 200 permits against their source PDFs. The sample was drawn from successfully processed permits with a fixed random seed, stratified by (i) jurisdiction (all states and territories in the dataset are represented) and (ii) document-length tercile based on extracted-text size (71 short, 69 medium, 60 long), as a proxy for permit complexity. For each sampled permit, reviewers compared every extracted general field and every field of every unit row to the corresponding section of the source document, recorded field-level correctness, and listed emission units present in the permit but absent from the extraction. We report precision (fraction of extracted values matching the reference), recall (fraction of reference values correctly extracted), and completeness (fraction of non-null values) by field group, plus unit identification accuracy and unit count error. The sampling script, sample manifest, and audit workbook are included in the repository.

[RESULTS PLACEHOLDER: the audit workbook (2,800 general-field lines and 18,584 unit-field lines) is prepared at `data/processed/validation/manual_audit/`; human review is pending. Insert overall accuracy, per-field-group precision/recall (Table 3), and unit count error distribution when complete.]

### 4.2 Multi-Model Cross-Validation

As an independent, scalable check on extraction reliability, the pipeline includes a multi-model validation mode in which the same permit text is independently extracted by three models from different providers, and the outputs are compared field by field after normalization (case-folding, whitespace collapsing, and numeric parsing). A permit is flagged for review when fewer than two models succeed, when any key general field has conflicting values, or when the unit-level agreement ratio falls below 70%. Emission units are aligned across models by unit identifier.

We applied this procedure to 202 randomly sampled permits across two panels that share the same two verifier models (Gemini 2.5 Flash and GLM-5), differing only in the third slot: gpt-oss-120b---a model that produced 27% of the released records---in one panel (n = 100), and Gemma 4 in the other (n = 102). Of the 202 sampled permits, 194 yielded usable comparisons (at least two models succeeded). An expired API gateway authorization midway through one panel run initially left 54 permits with fewer than two successful extractions; a follow-up re-validation run on the same panel recovered 47 of them. The eight permits that remain unusable are large, unit-heavy documents for which fewer than two models returned complete output within the token and timeout limits.

Agreement on structured general fields was high among permits where at least one model produced a value: facility state (99%), expiration date (99%), ZIP code (97%), city (97%), facility address (83%), issuance date (81%), permit number (78%), and facility name (76%, with disagreements dominated by formatting variants such as corporate suffixes). Agreement was lower for fields whose permit-text representation is inherently variable: permit type (43%), regulatory authority (36%, agency naming variants), and primary applicable regulations (15%, a free-text field where models phrase the same programs differently). At the unit level (1,052 unit-model comparisons), agreement was high for structured attributes---capacity value (94%), fuel type (85%), capacity unit (77%), NESHAP/NSPS subpart citations (69%)---and lower for free-text fields: control devices (65%), pollutants (45%), emission limits (40%), and unit description (34%). The per-permit unit agreement ratio averaged 0.75 (median 0.74), with 56% of permits meeting the 0.70 threshold and 23% fully consistent across all compared unit fields. An earlier set of runs with a different verifier panel (GPT-4.1 and Claude Sonnet alongside gpt-oss-120b; n = 61, March 2026) produced closely similar field-level agreement patterns, indicating that the agreement structure is stable across model panels.

Because exact matching on normalized strings treats representational variants (paraphrase, abbreviation, reordering) as disagreement, the field-level percentages above understate substantive agreement, particularly for free-text fields. To quantify this directly, we added a content-level adjudication step and applied it to a fresh random sample of 100 permits, extracted by a third panel that retains the two shared verifier models (Gemini 2.5 Flash and GLM-5) with Claude Sonnet 4.6 in the third slot. A fourth model that performed no extraction (gpt-oss-120b) reviews the three independent extractions for each permit together with the source permit text and classifies, field by field, whether the values agree on content---identical, equivalent (paraphrase, abbreviation, reordering, or unit/format differences), or compatible (one value more complete than another, without contradiction)---or genuinely conflict; for genuine conflicts it records which value the source supports. Of the 100 permits, 98 had at least two usable extractions. Content-level agreement on key general fields was 94% (861 of 918 field-level judgments), compared with 67% (616 of 917) under exact string matching---a 27-percentage-point gap corresponding to 244 field judgments that exact matching scored as disagreement but adjudication ruled equivalent in content (Table 4). The gap was concentrated in exactly the free-text fields that exact matching handles poorly: agreement on primary applicable regulations rose from 5% to 91%, regulatory authority from 24% to 100%, and permit type from 33% to 86%, while tightly formatted fields (facility state, city, ZIP code, and dates) were already near-unanimous and did not change. Emission-unit lists agreed on content for 81 of the 98 permits. Among the 57 field-level disagreements judged to be genuine conflicts, no single model was systematically correct: the source supported Gemini 2.5 Flash most often, but corrections were distributed across all three extractors, indicating that residual conflicts arise from document-specific ambiguity rather than a consistently weaker model.

Two properties of this comparison should be noted. First, the field-by-field statistics in the preceding paragraphs use exact matching on normalized strings; as the content-level adjudication confirms, disagreement on free-text fields predominantly reflects representational variation (paraphrase, ordering, abbreviation) rather than factual conflict, so the exact-match percentages are conservative lower bounds on cross-model agreement, and the manual audit (Section 4.1) assesses semantic correctness against the source directly. Second, because units are aligned by unit identifier, unit-ID agreement is definitionally perfect and is excluded from the reported metrics. Under the strict review gate---which flags a permit if any single key field disagrees---187 of 194 usable permits were flagged, driven almost entirely by the free-text regulations, permit-type, and regulatory-authority fields; consistent with the adjudication results, these flags reflect representational variation far more than substantive error, and the gate is intentionally conservative, serving as a triage mechanism rather than an accuracy estimate.

### 4.3 Automated Quality Control

Automated checks were applied to the full dataset (97,757 rows; 94,537 successful); the QC script and machine-readable report are included in the repository:

- **Format validation**: among successful rows with values, 99.98% of state abbreviations are valid two-letter codes (15 rows carry malformed values such as "QC" or "CA, WA") and 99.75% of ZIP codes match five- or nine-digit patterns. Issuance and expiration dates are in ISO 8601 format in 69.1% and 64.4% of cases respectively; the remainder predate the date-normalization step and are normalized in post-processing for release. [NOTE: re-apply date normalization and hours parsing to the full corpus before release---both are offline transformations---and update these figures.]
- **Completeness profiling**: missingness rates vary widely by field, reflecting what permits actually report: facility name and state are nearly always present, while rated efficiency (98.6% missing), generation capacity (94.3%), and annual run hours (90.2%) are sparse because most permits do not state them at the unit level.
- **Cross-field consistency**: 92.3% of the 51,842 rows with a capacity value also carry a capacity unit.
- **Duplicate detection**: no duplicated (source document, unit identifier) pairs exist among the 91,677 unit rows, and no "ERROR" sentinel values leak into successful rows.

### 4.4 Known Error Modes

Several systematic error modes are documented for user awareness:

- **OCR artifacts**: scanned permits may produce character substitutions in unit IDs, capacity values, or pollutant abbreviations (e.g., "NOx" misread as "N0x").
- **Unit boundary ambiguity**: some permits describe multiple pieces of equipment under a single unit ID or embed unit attributes within narrative paragraphs rather than structured tables, leading to under- or over-counting of distinct emission units.
- **Limit representation**: emission limits in permits are frequently conditional, time-averaged, or expressed in complex multi-clause forms; these are captured as free text and may require domain-specific parsing for quantitative use.
- **Address disambiguation**: the LLM may occasionally extract a corporate or mailing address rather than the physical facility address, particularly when the permit lists multiple addresses without clear labeling.
- **Temporal coverage**: the dataset reflects permits available at the time of acquisition and may include expired permits alongside current ones; users should filter on issuance and expiration dates for analyses requiring temporal specificity.

## 5. Usage Notes

### 5.1 Recommended Filtering

For analytical use, records should be filtered to `Status` values of "Success" (or "Success (No Units Found)" for permit-level analyses). Records with failure statuses should be retained when quantifying coverage or identifying documents for manual review or reprocessing.

### 5.2 Industry Classification

NAICS codes are extracted when present in the permit. Older permits and certain state formats report only SIC codes, which are reflected in the `Industry Description` field where stated; users performing industry-level analyses should expect NAICS coverage to vary by state and permit era. [NOTE: if the corpus is re-extracted with the expanded schema, SIC Code becomes a dedicated column and this note should be restored to the dual-classification version.]

### 5.3 Permit Program Scope

While the acquisition pipeline targets Title V operating permits, some agency portals co-locate other permit classes (state-only operating permits, synthetic minor permits, construction/PSD permits), and a fraction of acquired documents fall into these classes. The `Primary Applicable Regulations` field captures the regulatory program(s) cited in each permit; users focused exclusively on Title V sources should filter on this field, recognizing that it reflects permit language rather than a verified regulatory classification.

### 5.4 Emission Limits

Emission limits are captured as free text because permits frequently express limits in complex, conditional forms (e.g., rolling averages, startup/shutdown exceptions, fuel-specific limits). Multiple limits for a single unit are separated by semicolons. Users requiring numeric limit values should implement domain-specific parsing as a downstream step.

### 5.5 Linking to External Data

The `Facility Name`, `Facility Address`, `Permit Number`, and NAICS code can be used to link records to external registries such as the EPA's ECHO database, the Facility Registry Service, the National Emissions Inventory, or the Toxics Release Inventory. Users should expect identifier variation across sources and should implement fuzzy matching or use authoritative facility identifiers (e.g., EPA FRS ID) when available for cross-referencing.

### 5.6 Unit-Level Interpretation

Each unit row represents an emission unit as described in the permit, but unit definitions and identifiers are not standardized across jurisdictions. Some permits group multiple pieces of equipment under a single unit ID, while others list control devices separately. Unit counts should therefore be interpreted as permit-reported counts rather than physical equipment inventories.

## 6. Code Availability

All code for data collection, text extraction, LLM-based information extraction, and post-processing consolidation is available in the project's open-source GitHub repository. The repository is structured as an installable Python package (`permit_data_extraction`) with the following principal components:

- **Acquisition tooling**: the agentic portal crawler (`permit_data_extraction/pdf_downloader.py`), the EPA permit hub downloader, and 46 state-specific download scripts (under `scripts/`), covering EPA and more than 25 state and local agency portals.
- **Text extraction**: the extraction pipeline entry point (`run_extraction_pipeline.py`) with native PDF parsing and OCR fallback capabilities.
- **Information extraction**: the LLM-based extraction module (`permit_data_extraction/dataset.py`), containing the prompt template, schema definitions, post-processing logic, and output consolidation.
- **Testing and validation**: test suites for data generation and extraction quality assessment.

The repository README documents required dependencies (Python packages, Poppler, and Tesseract for OCR), environment configuration (API keys for LLM access), and commands to reproduce each pipeline stage. A specific release tag corresponding to the dataset version described in this paper is archived with a persistent identifier for long-term preservation.

## Acknowledgements

[To be completed.]

## Author Contributions

[To be completed.]

## Competing Interests

The authors declare no competing interests.

## Figures and Tables

**Figure 1.** Pipeline overview. Data flow from agency portals through the agentic crawler and state-specific downloaders to a PDF corpus, followed by text extraction (native parsing with OCR fallback), LLM-based information extraction, and consolidation into the structured tabular output.

**Figure 2.** Geographic coverage. Map of the United States showing the number of permits processed by state, with state-level counts and extraction success rates.

**Table 1.** Dataset schema summary. Column names, descriptions, data types, and example values for all 41 fields, organized by field group (processing/provenance, general permit, emission unit, file metadata).

**Table 2.** Coverage statistics. Number of source PDFs, extracted records, and extraction success rates by state, with indication of acquisition method (EPA hub, agentic crawler, or state-specific script).

**Table 3.** Technical validation results. Field-level accuracy from manual audit, stratified by field group and document type (born-digital versus OCR).

**Table 4.** Cross-model agreement on key general fields: exact string matching versus content-level adjudication. From a fresh 100-permit validation sample (98 with at least two usable extractions) extracted by a panel of Gemini 2.5 Flash, GLM-5, and Claude Sonnet 4.6. Exact = agreement after case-folding, whitespace collapsing, and numeric parsing among permits where at least two models produced a value. Content = an independent fourth model (gpt-oss-120b), which performed no extraction, judging the values equivalent in content (identical, paraphrase/format variant, or compatible) given the source text. "Flips" counts field judgments that exact matching scored as disagreement but content adjudication ruled equivalent.

| Field | Exact agreement | Content agreement | Flips |
|---|---|---|---|
| Facility name | 77% (74/96) | 96% (92/96) | 18 |
| Facility address | 82% (67/82) | 95% (78/82) | 11 |
| Facility city | 98% (89/91) | 98% (89/91) | 0 |
| Facility state | 100% (97/97) | 100% (97/97) | 0 |
| Facility ZIP code | 92% (70/76) | 96% (73/76) | 3 |
| Permit number | 77% (72/93) | 87% (81/93) | 9 |
| Permit type | 33% (32/96) | 86% (83/96) | 51 |
| Issuance date | 80% (41/51) | 80% (41/51) | 0 |
| Expiration date | 98% (46/47) | 98% (47/48) | 0 |
| Regulatory authority | 24% (23/97) | 100% (97/97) | 74 |
| Primary applicable regulations | 5% (5/91) | 91% (83/91) | 78 |
| **All key general fields** | **67% (616/917)** | **94% (861/918)** | **244** |

## References

> [NOTE: citations 6–18 below are drafted from memory and need verification of volume/page/DOI details before submission. Items marked VERIFY especially.]

1. U.S. Congress. Clean Air Act Amendments of 1990, Title V---Permits. 42 U.S.C. sections 7661--7661f (1990).
2. U.S. Environmental Protection Agency. Title V Operating Permits. https://www.epa.gov/title-v-operating-permits (accessed 2026).
3. U.S. Environmental Protection Agency. National Emissions Inventory (NEI). https://www.epa.gov/air-emissions-inventories/national-emissions-inventory-nei (accessed 2026).
4. U.S. Environmental Protection Agency. Toxics Release Inventory (TRI) Program. https://www.epa.gov/toxics-release-inventory-tri-program (accessed 2026).
5. U.S. Environmental Protection Agency. Enforcement and Compliance History Online (ECHO). https://echo.epa.gov/ (accessed 2026).
6. U.S. Environmental Protection Agency. Emissions & Generation Resource Integrated Database (eGRID). https://www.epa.gov/egrid (accessed 2026).
7. U.S. Environmental Protection Agency. Clean Air Markets Program Data. https://campd.epa.gov/ (accessed 2026).
8. U.S. Environmental Protection Agency. Facility Registry Service (FRS). https://www.epa.gov/frs (accessed 2026).
9. Banzhaf, S., Ma, L. & Timmins, C. Environmental justice: the economics of race, place, and pollution. *J. Econ. Perspect.* **33**, 185--208 (2019). [VERIFY]
10. Dagdelen, J. et al. Structured information extraction from scientific text with large language models. *Nat. Commun.* **15**, 1418 (2024). [VERIFY]
11. Polak, M. P. & Morgan, D. Extracting accurate materials data from research papers with conversational language models and prompt engineering. *Nat. Commun.* **15**, 1569 (2024). [VERIFY]
12. PyPDF2 Developers. PyPDF2: a pure-Python PDF library. https://pypi.org/project/PyPDF2/ (accessed 2026).
13. Smith, R. An overview of the Tesseract OCR engine. In *Proc. Ninth International Conference on Document Analysis and Recognition (ICDAR)* 629--633 (IEEE, 2007).
14. OpenAI. gpt-oss-120b & gpt-oss-20b model card. Preprint at https://arxiv.org/abs/2508.10925 (2025). [VERIFY]
15. Gemini Team, Google. Gemini: a family of highly capable multimodal models. Preprint at https://arxiv.org/abs/2312.11805 (2023). [VERIFY — cite the report matching the Gemini 2.0 models used]
16. U.S. Environmental Protection Agency. EJScreen: Environmental Justice Screening and Mapping Tool. https://www.epa.gov/ejscreen (accessed 2026). [Optional — cite if EJ use case is discussed]
17. Title V Task Force. *Final Report to the Clean Air Act Advisory Committee: Title V Implementation Experience* (U.S. EPA, 2006). [Optional — supports program-fragmentation claims; VERIFY]
18. Lawrence Berkeley National Laboratory. CBORG: AI portal and model API gateway. https://cborg.lbl.gov (accessed 2026). [If permitted to cite — discloses the API gateway used]
19. U.S. Census Bureau. Annual Survey of Manufactures (ASM), 2021. https://www.census.gov/programs-surveys/asm.html (accessed 2026).
20. U.S. Census Bureau. County Business Patterns (CBP), 2022. https://www.census.gov/programs-surveys/cbp.html (accessed 2026).
