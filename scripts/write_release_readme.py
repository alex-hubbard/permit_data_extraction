#!/usr/bin/env python3
"""Write README.md for a de-identified release directory and add it to the
manifest. Reads the manifest.json and data_dictionary.csv that
scripts/deidentify_release.py wrote, so the counts in the README are the ones
measured on the files.

Usage: python scripts/write_release_readme.py data/processed/release/v5g_deid
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

REPO_URL = "https://github.com/alex-hubbard/permit_data_extraction"
DOI = "10.5281/zenodo.23679528"


def main(out: Path) -> None:
    m = json.loads((out / "manifest.json").read_text())
    full, mfg = m["tables"]["full"], m["tables"]["manufacturing"]
    with (out / "data_dictionary.csv").open(encoding="utf-8") as f:
        cols = [r["column"] for r in csv.DictReader(f)]
    removed = ", ".join(c for c in m["removed_columns"] if c != "Latest Facility Filename")
    text = f"""# Equipment units extracted from U.S. Title V air permits (public release)

Release `{m['release']}`, built {m['built']} from `{m['source']}`.

DOI: https://doi.org/{DOI}

Each row describes one emission unit (a boiler, process heater, engine, tank, coating line, ...)
listed in a state or local Title V air permit, as extracted by large language models; or
records a permit document from which no units, or no text, could be extracted (see `Status`).

## Files

| File | Contents |
|---|---|
| `permit_equipment_manufacturing.csv.gz` | Manufacturing subset (`NAICS Resolved` in sectors 31-33): {mfg['rows']:,} rows from {mfg['documents']:,} permit documents and {mfg['facilities']:,} facilities |
| `permit_equipment_full.csv.gz` | Every processed permit, all sectors: {full['rows']:,} rows from {full['documents']:,} permit documents and {full['facilities']:,} facilities |
| `permit_equipment_full.parquet` | The full table in Parquet format |
| `per_state_manufacturing.zip`, `per_state_full.zip` | The same two tables split into one CSV per state (`unknown_state` holds rows with a blank or invalid state) |
| `data_dictionary.csv` | Description of each of the {len(cols)} columns |
| `manifest.json` | Row, document and facility counts per table and SHA-256 checksums of every file |

The two tables share one schema; the manufacturing table is the full table filtered on
`NAICS Resolved`. CSV files are UTF-8 with a header row.

## De-identification

Facility descriptors are limited to state, county and industry codes (NAICS, SIC).
Removed columns: {removed}. In their place:

- `Document ID` identifies the source permit document (all rows from one document share it).
- `Facility ID` identifies the facility (same standardized facility name, state and ZIP code);
  it is blank when the permit names no facility. Distinct `Facility ID` values reproduce the
  facility counts above.
- `Latest Facility Document ID` replaces the filename of the document kept for the facility in
  the deduplication.

Both IDs are keyed hashes; they are stable across releases built with the same key and cannot
be reversed from the published data. In free-text columns (for example `Industry Description`
and `Unit Description`), each row's own facility name, owner name and street address were
replaced with `[facility]`, `[owner]` and `[address]` ({m['free_text_scrub']['cells_changed']:,} cells).

This removes names; it does not anonymize. A county combined with a six-digit NAICS code can
still point to a single plant, and Title V permits are themselves public records.

## Usage notes

- `Status` = `Success` rows are equipment units. `Success (No Units Found)` rows stand for a
  permit in which no units were found, and `LLM Extraction Failed` / `Text Reading Failed` rows
  record failed attempts; filter on `Status` to remove them.
- For analyses that depend on completeness, exclude documents with a non-zero `Pieces Failed`.
- Unit counts are what the permit lists, not an inventory of physical equipment: some permits
  group several pieces of equipment under one unit, and others list control devices as units.
- `Equipment Category`, `Equipment Subcategory` and `Equipment Designation` (combustion /
  non-combustion / unknown) are rule-based rollups of
  the free-text `Unit Type`.

## Code

The extraction pipeline and the scripts that build this release
(`scripts/deidentify_release.py`) are at {REPO_URL}.
"""
    readme = out / "README.md"
    readme.write_text(text, encoding="utf-8")
    m["files"] = [f for f in m["files"] if f["file"] != "README.md"]
    m["files"].append({"file": "README.md", "bytes": readme.stat().st_size,
                       "sha256": hashlib.sha256(readme.read_bytes()).hexdigest()})
    (out / "manifest.json").write_text(json.dumps(m, indent=2))
    print(f"wrote {readme} ({len(cols)} columns documented)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
