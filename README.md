# permit_data_extraction

Extracting relevant information from air permits using LLMs.

This project provides a pipeline for downloading, processing, and extracting structured data from EPA and state-level air quality permits. It uses OCR and large language models to convert permit PDFs into machine-readable datasets.

## Project Organization

```
├── .env.example                  <- Template for required environment variables
├── download_epa_permits.py       <- Download EPA Final Permits from permit hub
├── run_extraction_pipeline.py    <- Run the full text extraction pipeline
├── standalone_test_runner.py     <- Run the extraction accuracy test suite
│
├── permit_data_extraction/       <- Main Python package
│   ├── config.py                 <- Project paths and configuration
│   ├── dataset.py                <- Data loading utilities
│   ├── ocr.py                    <- PDF text extraction (OCR)
│   ├── epa_pdf_downloader.py     <- Selenium-based EPA permit downloader
│   ├── generic_permit_downloader.py  <- Generic permit PDF downloader
│   ├── pdf_downloader.py         <- Base PDF download utilities
│   ├── state_permit_scraper.py   <- State-specific permit scrapers
│   ├── analyze_permit_data.py    <- Permit data analysis
│   ├── analyze_facilities.py     <- Facility-level analysis
│   ├── analyze_field_categories.py <- Field category analysis
│   ├── map_air_facilities.py     <- Air facility mapping
│   ├── map_all_manufacturing_facilities.py <- Manufacturing facility mapping
│   ├── map_permit_links_by_state.py <- Permit link geographic mapping
│   ├── processed_facilities_map.py  <- Processed facility map generation
│   └── modeling/                 <- LLM extraction and training
│       ├── predict.py            <- Run model inference
│       └── train.py              <- Model training utilities
│
├── scripts/                      <- State-specific download and scraping scripts
├── notebooks/                    <- Jupyter notebooks for exploration and analysis
├── tests/                        <- Test suite for extraction accuracy
├── docs/                         <- MkDocs documentation site
│   └── docs/
│       ├── nature_data_schema.md         <- Dataset schema documentation
│       ├── nature_data_validation.md     <- Data validation methodology
│       ├── nature_data_pipeline_mapping.md <- Pipeline architecture
│       ├── nature_data_release.md        <- Data release notes
│       └── nature_data_paper_outline.md  <- Nature data paper outline
├── reports/figures/              <- Generated figures and maps
├── pyproject.toml                <- Package metadata and tool configuration
├── requirements.txt              <- Python dependencies
└── requirements-test.txt         <- Test dependencies
```

## Setup

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd permit_data_extraction
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

## Usage

### Download EPA permits
```bash
# Download all permits
python download_epa_permits.py

# Download first 10 (for testing)
python download_epa_permits.py --max 10

# Resume from a specific row
python download_epa_permits.py --resume 100
```

### Run the extraction pipeline
```bash
python run_extraction_pipeline.py
```

### Run tests
```bash
python standalone_test_runner.py
```

## Documentation

Full documentation is available via MkDocs:
```bash
mkdocs serve
```

See `docs/docs/` for the Nature data paper documentation including dataset schema, validation methodology, and pipeline architecture.

## License

See [LICENSE](LICENSE) for details.
