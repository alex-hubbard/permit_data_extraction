#!/usr/bin/env markdown
# Dataset Schema And Data Dictionary

This dataset is stored as a tabular file at `data/processed/permit_data_extracted.xlsx`.
Each row corresponds to either one emission unit extracted from a permit or a single
row for a permit with no units identified. The `Filename` column is the primary
linking key to the original permit PDF and extracted text.

## Table: `permit_data_extracted`

### Record Structure

- **One row per emission unit** when units are present.
- **One row per permit** when no units are found; unit-level fields are `null`.
- **Repeat of general permit fields** across all unit rows for the same permit.

### Core Metadata Columns

| Column | Description |
| --- | --- |
| `Filename` | Source permit filename (without extension). |
| `Status` | Processing status (e.g., `Success`, `Success (No Units Found)`, `LLM Extraction Failed`). |
| `Processing Date` | Date the extraction was run (YYYY-MM-DD). |
| `Model Used` | LLM model identifier used for extraction. |
| `Spec Sheet Link` | Optional link to a supplemental equipment spec sheet (if available). |

### General Permit Fields

| Column | Description |
| --- | --- |
| `Facility Name` | Name of the permitted facility. |
| `Owner/Operator Name` | Legal entity that owns or operates the facility, if different from Facility Name. |
| `Facility Address` | Street address of the facility. |
| `Facility City` | City where the facility is located. |
| `Facility State Abbreviation` | Two-letter state abbreviation. |
| `Facility Zip Code` | ZIP or postal code. |
| `Facility County` | County name. |
| `NAICS Code` | NAICS industry classification code. |
| `SIC Code` | Standard Industrial Classification code (digits only, e.g., 3295). |
| `Operating Hours` | Reported operating hours or schedule. |
| `Industry Description` | Narrative industry or process description. |
| `Permit Number` | Permit identifier assigned by the agency. |
| `Permit Type` | Permit classification (e.g., Title V, State Only Operating Permit, Synthetic Minor). |
| `Issuance Date` | Permit issuance date. |
| `Expiration Date` | Permit expiration date. |
| `Regulatory Authority` | Issuing authority (state agency or EPA). |
| `Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)` | Primary regulatory program(s) cited in the permit. |

### Emission Unit Fields

| Column | Description |
| --- | --- |
| `Unit ID` | Emission unit identifier from the permit. |
| `Unit Description` | Short description of the emission unit. |
| `Unit Quantity` | Count of units if multiple are grouped. |
| `Unit Make` | Manufacturer or make. |
| `Unit Model` | Model or series. |
| `Year of Manufacture` | Year the unit was manufactured. |
| `Unit Type` | Equipment type (boiler, kiln, furnace, etc.). |
| `Pollutants` | Pollutants associated with the unit. |
| `Emission Limits` | Emission limits as listed in the permit (text). |
| `Opacity Limit` | Visible emission limit (e.g., 20%, or 10% 3 min/hr; 30% any time). |
| `Throughput/Production Limit` | Material processing or production rate limits (e.g., 500 tons/day). |
| `Control Device(s)` | Control devices associated with the unit. |
| `Capacity Value` | Numeric capacity value. |
| `Capacity Unit` | Capacity unit (e.g., MMBtu/hr, tons/year). |
| `Fuel Type` | Fuel type (e.g., natural gas, coal). |
| `Rated Efficiency` | Efficiency rating (e.g., 90%). |
| `Annual Run Hours` | Annual operating hours. |
| `Generation Capacity` | Electrical generation capacity (if applicable). |
| `Applicable NESHAP/NSPS Subpart` | Specific federal standard(s) applicable to this unit (e.g., 40 CFR 63 Subpart DDDDD). |

## Missingness And Error Conventions

- Missing values are stored as `null`/empty cells.
- If an extraction fails, fields may be populated with `ERROR` and the `Status` will
  indicate the failure mode.
