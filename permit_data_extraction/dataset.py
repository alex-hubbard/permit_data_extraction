from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import threading
import time  # To add delays between API calls if needed
import shutil
import tempfile
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import openai
import pandas as pd
import PyPDF2  # Library for reading text from PDFs
import typer
from dotenv import dotenv_values
from loguru import logger
from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from permit_data_extraction.config import RAW_DATA_DIR, INTERIM_DATA_DIR, PROCESSED_DATA_DIR

app = typer.Typer()

OPENAI_API_KEY = dotenv_values()['CBORG_API_KEY']

TEXT_INPUT_DIR = INTERIM_DATA_DIR / 'extracted_text'
PROCESSING_LOG_PATH = INTERIM_DATA_DIR / "processed_files_log.jsonl"
PROCESSING_LOG_LOCK = threading.Lock()

# Path for the combined output Excel file (all runs merged)
OUTPUT_EXCEL_FILE = os.path.join(PROCESSED_DATA_DIR,
                                 'permit_data_extracted.xlsx')

# Run-specific output: each run writes to permit_data_YYYY-MM-DD_HH-MM-SS_v0.0.1.xlsx
def _get_version():
    try:
        return _pkg_version("permit_data_extraction").replace(".", "-")
    except Exception:
        return "0-0-1"


def get_run_output_excel_path():
    """Path for this run's Excel file (date + time + version)."""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H-%M-%S")
    ver = _get_version()
    name = f"permit_data_{date_str}_{time_str}_v{ver}.xlsx"
    return Path(PROCESSED_DATA_DIR) / name

# Save tuning
SAVE_EVERY_N_FILES = 100  # Reduce Excel IO by batching writes
# LLM concurrency tuning
LLM_MAX_WORKERS = 4

# Feature flags
ENABLE_SPEC_SHEET_LOOKUP = False

# LLM model configuration
LLM_MODEL = "gemini-2.0-flash-lite"  # 1M context, $0.10/1M input tokens
LLM_LARGE_MODEL = os.getenv("LLM_LARGE_MODEL", "gemini-2.0-flash-lite")  # 1M context — fallback for docs that exceed primary model context


def _env_flag(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Retry with LLM_LARGE_MODEL on token-limit errors (separate from --retry-failed)
ALLOW_LARGE_MODEL_RETRY = _env_flag("PERMIT_EXTRACTION_ALLOW_LARGE_MODEL_RETRY", True)

# Validation pipeline configuration
VALIDATION_SAMPLE_SIZE = 20
VALIDATION_OUTPUT_DIR = PROCESSED_DATA_DIR / "validation"
VALIDATION_MODEL_OPENAI = os.getenv("VALIDATION_MODEL_OPENAI", "openai/gpt-4.1")
VALIDATION_MODEL_ANTHROPIC = os.getenv("VALIDATION_MODEL_ANTHROPIC", "amazon/claude-sonnet-4-6")
VALIDATION_MODELS = [LLM_MODEL, VALIDATION_MODEL_OPENAI, VALIDATION_MODEL_ANTHROPIC]
# Key fields used for consistency/completeness comparison (moderate strictness)
KEY_GENERAL_FIELDS = [
    "Facility Name",
    "Facility Address",
    "Facility City",
    "Facility State Abbreviation",
    "Facility Zip Code",
    "Permit Number",
    "Permit Type",
    "Issuance Date",
    "Expiration Date",
    "Regulatory Authority",
    "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)",
]
KEY_UNIT_FIELDS = [
    "Unit ID",
    "Unit Description",
    "Pollutants",
    "Emission Limits",
    "Control Device(s)",
    "Capacity Value",
    "Capacity Unit",
    "Fuel Type",
    "Applicable NESHAP/NSPS Subpart",
]
VALIDATION_UNIT_AGREEMENT_THRESHOLD = 0.70  # fraction of KEY_UNIT_FIELDS that must be consistent per unit

# General permit info — lead = facility/site context; trail = permit identifiers (also placed at end of Excel)
GENERAL_TARGET_FIELDS_LEAD = [
    "Facility Name",
    "Owner/Operator Name",
    "Facility Address",
    "Facility City",
    "Facility State Abbreviation",
    "Facility Zip Code",
    "Facility County",
    "NAICS Code",
    "SIC Code",
    "Operating Hours",  # facility: site schedule / hours of operation (not per-equipment runtime)
    "Industry Description",
]
GENERAL_TARGET_FIELDS_TRAIL = [
    "Permit Number",
    "Permit Type",
    "Issuance Date",
    "Expiration Date",
    "Regulatory Authority",
    "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)",
]
GENERAL_TARGET_FIELDS = GENERAL_TARGET_FIELDS_LEAD + GENERAL_TARGET_FIELDS_TRAIL

# Specific fields for each emission unit (LLM output only — POST_PROCESS_FIELDS are filled after extraction)
UNIT_DETAIL_FIELDS = [
    "Unit ID",
    "Unit Description",
    "Unit Quantity",
    "Unit Make",
    "Unit Model",
    "Year of Manufacture",
    "Unit Type", # especially for boilers, furnaces, etc.
    "Pollutants",  # Could be a list or comma-separated string
    "Emission Limits",  # Could be complex; aim for text description for now
    "Opacity Limit",  # visible emission limit, e.g., "20%" or "10% (3 min/hr); 30% (any time)"
    "Throughput/Production Limit",  # material processing or production rate limits
    "Control Device(s)",
    "Capacity Value",  # e.g., MMBtu/hr, tons/year
    "Capacity Unit",  # e.g., MMBtu/hr, tons/year
    "Fuel Type",  # e.g., Natural Gas, Coal, etc.
    "Rated Efficiency", # e.g., 90%
    "Annual Run Hours",  # equipment: this unit's annual hours of operation / expected runtime
    "Generation Capacity", # e.g., 100 MW
    "Applicable NESHAP/NSPS Subpart",  # specific federal standard per unit
]

# Filled in Python after LLM extraction (not requested from the model)
POST_PROCESS_FIELDS = [
    "Operating Hours Value",
    "Operating Hours Time Basis",
    "Annual Run Hours Value",
    "Annual Run Hours Time Basis",
]

# All fields expected in each output row (LLM general + LLM units + post-processed)
ALL_OUTPUT_FIELDS = (
    GENERAL_TARGET_FIELDS_LEAD
    + UNIT_DETAIL_FIELDS
    + POST_PROCESS_FIELDS
    + GENERAL_TARGET_FIELDS_TRAIL
)

# Excel column header overrides (internal key -> analyst-facing label)
EXCEL_COLUMN_DISPLAY_NAMES = {
    "Model Used": "Model Used (AI extraction model)",
    "Owner/Operator Name": "Owner/Operator Name (if different from Facility Name)",
    "SIC Code": "SIC Code (Standard Industrial Classification)",
    "Permit Type": "Permit Type (e.g. Title V, State Only, Synthetic Minor)",
    "Operating Hours": "Operating Hours (facility-wide schedule)",
    "Operating Hours Value": "Operating Hours — numeric/value portion",
    "Operating Hours Time Basis": "Operating Hours — unit or basis (e.g. hours/year)",
    "Annual Run Hours": "Annual Run Hours (this emission unit)",
    "Annual Run Hours Value": "Annual Run Hours — numeric/value portion",
    "Annual Run Hours Time Basis": "Annual Run Hours — unit or basis (e.g. hours/year)",
    "Rated Efficiency": "Rated Efficiency (control/destruction/capture or equipment thermal — as stated)",
    "Opacity Limit": "Opacity Limit (visible emission limit for this unit)",
    "Throughput/Production Limit": "Throughput/Production Limit (material processing limits)",
    "Applicable NESHAP/NSPS Subpart": "Applicable NESHAP/NSPS Subpart (federal standard per unit)",
}

MISSING_INFO_PLACEHOLDER = "information not provided"


def _prompt_example_json_str() -> str:
    """Example JSON for PROMPT_TEMPLATE: every general and unit key present."""
    ex_general = {
        "Facility Name": "Example Plant",
        "Owner/Operator Name": "Example Manufacturing Corp",
        "Facility Address": "123 Industrial Way",
        "Facility City": "Exampleville",
        "Facility State Abbreviation": "OH",
        "Facility Zip Code": "43215",
        "Facility County": "Franklin",
        "NAICS Code": "331110",
        "SIC Code": "3312",
        "Operating Hours": "24/7 except annual maintenance",
        "Industry Description": "Iron and steel manufacturing",
        "Permit Number": "123-ABC",
        "Permit Type": "Title V",
        "Issuance Date": "2020-01-15",
        "Expiration Date": "2025-01-14",
        "Regulatory Authority": "Ohio EPA",
        "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)": (
            "Title V; 40 CFR Part 63 Subpart XXXXX"
        ),
    }
    assert set(ex_general) == set(GENERAL_TARGET_FIELDS)

    def _unit_row(overrides: dict) -> dict:
        row = {f: None for f in UNIT_DETAIL_FIELDS}
        row.update(overrides)
        assert set(row) == set(UNIT_DETAIL_FIELDS)
        return row

    u1 = _unit_row(
        {
            "Unit ID": "EU001",
            "Unit Description": "Natural Gas Boiler 1",
            "Unit Quantity": "1",
            "Unit Make": "BoilerWorks",
            "Unit Model": "BW-500",
            "Year of Manufacture": "2015",
            "Unit Type": "Boiler",
            "Pollutants": "NOx; CO; PM",
            "Emission Limits": "NOx: 0.05 lb/MMBtu; CO: 50 ppmvd",
            "Opacity Limit": "20%",
            "Throughput/Production Limit": None,
            "Control Device(s)": "Low NOx burner",
            "Capacity Value": "100",
            "Capacity Unit": "MMBtu/hr",
            "Fuel Type": "Natural gas",
            "Rated Efficiency": "85%",
            "Annual Run Hours": "4000",
            "Generation Capacity": None,
            "Applicable NESHAP/NSPS Subpart": "40 CFR 63 Subpart DDDDD",
        }
    )
    u2 = _unit_row(
        {
            "Unit ID": "EU002",
            "Unit Description": "Paint Booth A",
            "Unit Quantity": "1",
            "Unit Make": None,
            "Unit Model": None,
            "Year of Manufacture": None,
            "Unit Type": "Painting",
            "Pollutants": "VOC; HAPs",
            "Emission Limits": "VOC: 2.7 tons/year",
            "Opacity Limit": None,
            "Throughput/Production Limit": "500 gallons/day",
            "Control Device(s)": "Dry filters",
            "Capacity Value": None,
            "Capacity Unit": None,
            "Fuel Type": None,
            "Rated Efficiency": None,
            "Annual Run Hours": None,
            "Generation Capacity": None,
            "Applicable NESHAP/NSPS Subpart": None,
        }
    )
    payload = {**ex_general, "Emission Units": [u1, u2]}
    return json.dumps(payload, indent=2, ensure_ascii=False)


# --- LLM Prompt Template ---
# Requesting JSON output makes parsing much easier.
PROMPT_TEMPLATE = (
    """
Analyze the following text from an industrial air permit document. Your goal is to extract key permit information AND details about individual emission units.

**Instructions:**

1.  **Extract General Information:** Identify the following general details for the permit:
    * """
    + ", ".join(GENERAL_TARGET_FIELDS)
    + """

2.  **Extract Emission Unit Details:** Identify each distinct permitted emission unit mentioned in the text. For each unit, extract the following details:
    * """
    + ", ".join(UNIT_DETAIL_FIELDS)
    + """
    * **Important:** Look for information in sections describing specific equipment, process lines, or in tables summarizing emission sources.
    * **One row per distinct permitted source:** Create one object per distinct emission unit or permitted source the document clearly identifies. If the text only gives a combined limit for multiple units, use a single unit object with a **Unit Description** that states the combined scope, or split into multiple units only when the document explicitly breaks them out.

3.  **Output Format:** Present the extracted information in a single, valid JSON object.
    * The general information must be top-level key-value pairs.
    * The emission unit details must be in a JSON array named "Emission Units". Each array element is one object with **every** unit field listed above.
    * Use the field names **exactly** as listed above as JSON keys (including spelling and punctuation).
    * **Missing values:** Use JSON `null` for any general or unit field that is not present or cannot be determined from the permit text. Do not use empty strings for missing data.
    * **No fabrication:** Extract only information that is explicitly stated or clearly implied in the permit text below. Do **not** infer permit numbers, dates, equipment, or limits from general knowledge. If unsure, use `null`.
    * **Dates:** For **Issuance Date** and **Expiration Date**, use ISO 8601 calendar dates only (`YYYY-MM-DD`). If a date cannot be found or is illegible, use `null`.
    * **Facility location vs. corporate address:** **Facility Address**, **Facility City**, **Facility State Abbreviation**, **Facility Zip Code**, and **Facility County** must describe the **physical site of the permitted emission sources** (where the regulated equipment operates). Do **not** use a parent company headquarters, corporate office, or general business mailing address unless the permit text clearly identifies that address as the permitted facility site.
    * **Geography:** **Facility State Abbreviation** must be a two-letter US postal abbreviation in uppercase. **Facility Zip Code** must be a string (five digits or ZIP+4, e.g. `12345` or `12345-6789`).
    * **NAICS Code:** Use digits only, no hyphens (e.g. `331110`), or `null` if absent.
    * **SIC Code:** Use digits only (e.g. `3295`), or `null` if absent. Many permits list SIC instead of or alongside NAICS — extract whichever is present.
    * **Owner/Operator Name:** The legal entity that owns or operates the facility, if stated separately from the facility/plant name. Often appears in an "Owner Information" or "Responsible Official" section. Use `null` if the owner is the same as the facility name or not stated.
    * **Permit Type:** The classification of the permit — e.g. `Title V`, `State Only Operating Permit`, `Synthetic Minor`, `PSD`, `NSR`. Usually stated prominently on the cover page.
    * **Opacity Limit:** The visible emission limit for this unit or group, e.g. `20%` or `10% (3 min/hr); 30% (any time)`. Extract the numeric limit(s) as stated; use `null` if no opacity or visible emission limit applies to this unit.
    * **Throughput/Production Limit:** Material processing, production rate, or throughput limits for this unit (e.g. `500 tons/day`, `10,000 gallons/year`, `200 tons coal/hr`). These are operational limits on how much material the unit can process, distinct from emission limits. Use `null` if none stated.
    * **Applicable NESHAP/NSPS Subpart:** The specific federal standard(s) that apply to this emission unit (e.g. `40 CFR 63 Subpart DDDDD`, `40 CFR 60 Subpart IIII`). Extract the CFR citation and subpart letter(s) as stated; use semicolons to separate multiple standards. Use `null` if no NESHAP or NSPS applies to this unit.
    * **Pollutants** and **Emission Limits:** Use a **semicolon** (`;`) to separate multiple pollutants or multiple limit clauses so downstream parsing is consistent (e.g. `NOx; CO; PM` and `NOx: 0.05 lb/MMBtu; CO: 50 ppmvd`).
    * **Capacity:** Put the numeric or primary value in **Capacity Value** and the unit of measure in **Capacity Unit** (e.g. `MMBtu/hr`, `tons/year`). Do not merge unrelated numbers into one field. If **Capacity Value** is missing but capacity appears inside **Unit Description** (e.g. `50 MMBtu/hr boiler`, `3 MW generator`), copy the numeric value into **Capacity Value**, the unit into **Capacity Unit**, and keep the full text in **Unit Description**.
    * **Facility Address vs. city/state:** **Facility Address** should be **street address only** (number and street, suite if given). Do **not** repeat **Facility City**, **Facility State Abbreviation**, or **Facility Zip Code** in the address line.
    * **Facility Name vs. address:** **Facility Name** must be the operating name of the permitted site or emission source, not a street address, not a lessor/landlord LLC line, and not a mailing-only c/o line unless no true facility name exists.
    * **Rated Efficiency:** In permits this may mean **control device destruction/removal/capture efficiency**, **oxidizer thermal efficiency**, **filter/collection efficiency**, or **equipment thermal/fuel efficiency**—these differ. Extract the value **exactly as stated** and prefer short clarifying context if the permit ties it to a control device vs. boiler/heater thermal efficiency. Use a **percent** with `%` when the permit gives a percent; use decimals **only** when the permit states a decimal (e.g. 0.995); do not mix formats in one field.
    * **Internal references:** Do **not** copy PDF page/line cross-references (e.g. `b1743`, `b1743-b1744`, `S2239 - s2245`) into **Facility Name**, **Unit Description**, or other fields unless that text is itself the formal equipment tag in the permit.
    * **Fuel Type:** Prefer normalized names when the permit is clear, e.g. `#2 fuel oil` (not `#2 Oil` vs `#2 Fuel Oil` variants), `natural gas`, `diesel`, `propane`, `coal`, `wood`—pick one conventional label and stay consistent within this extraction.
    * **Operating Hours vs. Annual Run Hours:** **Operating Hours** is **facility-level**: the permitted site’s operating schedule or hours of operation (e.g. continuous, seasonal, shifts). **Annual Run Hours** is **equipment-level** (per emission unit): that unit’s annual operating hours, expected runtime, or utilization—only for the specific source described in that unit row. Do not put facility-wide site hours into **Annual Run Hours** unless they are explicitly stated for that unit; do not put per-unit runtime into **Operating Hours**. Give **Annual Run Hours** as a number when possible (e.g. `8760`); you may include `hours/year` in the same field if the permit states it that way.
    * If NO emission units are clearly identified, provide an empty array `[]` for "Emission Units".

**Example JSON Output Structure:**

"""
    + _prompt_example_json_str()
    + """

**Permit Text:**
--- START TEXT ---
{permit_text}
--- END TEXT ---

**JSON Output:**
"""
)


# --- Excel layout, post-processing, multi-sheet export ---

FUEL_TYPE_SYNONYMS = {
    "#2 oil": "#2 fuel oil",
    "#2 fuel oil": "#2 fuel oil",
    "#2 distillate": "#2 fuel oil",
    "no. 2 oil": "#2 fuel oil",
    "no. 2 fuel oil": "#2 fuel oil",
    "number 2 oil": "#2 fuel oil",
    "number 2 fuel oil": "#2 fuel oil",
    "diesel fuel": "diesel",
    "diesel oil": "diesel",
    "natural gas": "natural gas",
    "nat gas": "natural gas",
    "ng": "natural gas",
    "propane gas": "propane",
    "lp gas": "propane",
    "lpg": "propane",
}


def build_excel_column_order():
    """Analyst-oriented column order: tracking, facility, units + parsed hour columns, permit trail, file metadata."""
    lead = list(GENERAL_TARGET_FIELDS_LEAD)
    i = lead.index("Operating Hours") + 1
    lead_exp = lead[:i] + ["Operating Hours Value", "Operating Hours Time Basis"] + lead[i:]

    unit = list(UNIT_DETAIL_FIELDS)
    i = unit.index("Annual Run Hours") + 1
    unit_exp = unit[:i] + ["Annual Run Hours Value", "Annual Run Hours Time Basis"] + unit[i:]

    prefix = ["Status", "Processing Date", "Model Used"]
    trail = list(GENERAL_TARGET_FIELDS_TRAIL)
    suffix = ["Filename", "Spec Sheet Link", "Duplicate Equipment Documents", "Latest Facility Filename"]
    return prefix + lead_exp + unit_exp + trail + suffix


def _digits_naics(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return ""
    if "." in s and s.replace(".", "").isdigit():
        s = s.split(".")[0]
    return re.sub(r"\D", "", s)


def is_manufacturing_naics_3133(val) -> bool:
    """True if the code is manufacturing under NAICS (sectors 31/32/33) or SIC (2000-3999).

    The classification column may contain either NAICS or SIC codes. SIC codes are
    always exactly 4 digits; longer codes are treated as NAICS.
    """
    d = _digits_naics(val)
    if not d:
        return False
    if len(d) == 4:
        return d[0] in ("2", "3")
    if len(d) >= 2:
        return d[:2] in ("31", "32", "33")
    return False


def _strip_internal_reference_tokens(text: str) -> str:
    """Remove PDF-style letter+digit cross-references from free text."""
    if not text or not isinstance(text, str):
        return text
    s = text
    s = re.sub(r"\b[a-zA-Z]\d{3,5}\s*[-–]\s*[a-zA-Z]?\d{3,5}\b", " ", s)
    s = re.sub(r"\b[a-zA-Z]\d{3,5}\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_zip_code(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    digits = re.sub(r"[^\d]", "", s)
    if len(digits) >= 9:
        return f"{digits[:5]}-{digits[5:9]}"
    if len(digits) == 5:
        return digits
    if len(digits) > 5:
        return f"{digits[:5]}-{digits[5:9]}" if len(digits) >= 9 else digits[:5]
    return s if s else None


def _normalize_iso_date(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if hasattr(val, "strftime"):
        try:
            return val.strftime("%Y-%m-%d")
        except Exception:
            pass
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    dt = pd.to_datetime(s, errors="coerce")
    if pd.isna(dt):
        return None
    return dt.strftime("%Y-%m-%d")


def _split_hours_field(raw):
    """
    Split strings like '8760 hours/year' or '8,760 hr/yr' into (value_str, basis_str).
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return None, None
    basis = None
    m = re.search(
        r"(hours?/year|hours?/yr|hrs?/yr|h/yr|hr/yr|hours?\s+per\s+year|hrs?/year)",
        s,
        re.I,
    )
    if m:
        basis = m.group(1).lower().replace(" ", "")
        if "peryear" in basis:
            basis = "hours/year"
        elif basis.startswith("hr") and "yr" in basis:
            basis = "hours/year"
    num_m = re.search(r"([0-9,]+(?:\.[0-9]+)?)", s.replace(",", ""))
    val = None
    if num_m:
        try:
            val = str(float(num_m.group(1).replace(",", "")))
            if val.endswith(".0") and "." in val:
                val = val[:-2]
        except ValueError:
            val = num_m.group(1)
    return val, basis


def _infer_capacity_from_description(description, cap_val, cap_unit):
    """If capacity missing, try to pull number + unit from unit description."""
    if cap_val not in (None, "", []) and str(cap_val).strip():
        return cap_val, cap_unit
    if not description or not isinstance(description, str):
        return cap_val, cap_unit
    d = description
    patterns = [
        r"([0-9,]+(?:\.[0-9]+)?)\s*(MMBtu/hr|MMBtu/h|MMBtu|mmBtu/hr)\b",
        r"([0-9,]+(?:\.[0-9]+)?)\s*(MW|kw|kW)\b",
        r"([0-9,]+(?:\.[0-9]+)?)\s*(hp|HP|bhp)\b",
        r"([0-9,]+(?:\.[0-9]+)?)\s*(cfm|CFM|scfm|SCFM)\b",
        r"([0-9,]+(?:\.[0-9]+)?)\s*(tons/day|tons/yr|tons/year|lb/hr|lb/hour)\b",
    ]
    for pat in patterns:
        m = re.search(pat, d, re.I)
        if m:
            num = m.group(1).replace(",", "")
            unit = m.group(2)
            return num, unit
    return cap_val, cap_unit


def _canonicalize_fuel_type(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s:
        return None
    key = s.lower().strip()
    if key in FUEL_TYPE_SYNONYMS:
        return FUEL_TYPE_SYNONYMS[key]
    for k, v in FUEL_TYPE_SYNONYMS.items():
        if k in key or key in k:
            return v
    return s


def _title_case_city(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s:
        return None
    if s.isupper() and len(s) > 3:
        return s.title()
    return s


def postprocess_extraction_row(row: dict) -> dict:
    """
    Normalize dates/zips, fill missing facility operating hours, parse hour fields,
    infer capacity from description, strip PDF refs, canonicalize fuel, optional city casing.
    """
    out = dict(row)
    status = str(out.get("Status", "") or "")

    for k in ("Issuance Date", "Expiration Date"):
        if k in out:
            out[k] = _normalize_iso_date(out.get(k))

    if "Facility Zip Code" in out:
        out["Facility Zip Code"] = _normalize_zip_code(out.get("Facility Zip Code"))

    if "Facility City" in out:
        out["Facility City"] = _title_case_city(out.get("Facility City"))

    oh = out.get("Operating Hours")
    if str(oh).strip().upper() in ("ERROR", "INVALID UNIT ENTRY"):
        out["Operating Hours Value"] = None
        out["Operating Hours Time Basis"] = None
    elif oh is None or (isinstance(oh, float) and pd.isna(oh)) or str(oh).strip() == "":
        if status.startswith("Success"):
            out["Operating Hours"] = MISSING_INFO_PLACEHOLDER
            out["Operating Hours Value"] = None
            out["Operating Hours Time Basis"] = None
        else:
            out["Operating Hours Value"] = None
            out["Operating Hours Time Basis"] = None
    else:
        ov, ob = _split_hours_field(oh)
        out["Operating Hours Value"] = ov
        out["Operating Hours Time Basis"] = ob if ob else (
            "hours/year" if ov and "hour" in str(oh).lower() else None
        )

    ar = out.get("Annual Run Hours")
    av, ab = _split_hours_field(ar)
    out["Annual Run Hours Value"] = av
    out["Annual Run Hours Time Basis"] = ab

    ud = out.get("Unit Description")
    if isinstance(ud, str):
        out["Unit Description"] = _strip_internal_reference_tokens(ud)
    for fk in ("Facility Name", "Facility Address"):
        if isinstance(out.get(fk), str):
            out[fk] = _strip_internal_reference_tokens(out[fk])

    cv_raw = out.get("Capacity Value")
    cu_raw = out.get("Capacity Unit")
    cap_empty = cv_raw is None or (isinstance(cv_raw, float) and pd.isna(cv_raw)) or not str(cv_raw).strip()
    unit_empty = cu_raw is None or (isinstance(cu_raw, float) and pd.isna(cu_raw)) or not str(cu_raw).strip()
    cv, cu = _infer_capacity_from_description(out.get("Unit Description"), cv_raw, cu_raw)
    if cap_empty and cv not in (None, "") and str(cv).strip():
        out["Capacity Value"] = cv
    if unit_empty and cu not in (None, "") and str(cu).strip():
        out["Capacity Unit"] = cu

    if "Fuel Type" in out:
        out["Fuel Type"] = _canonicalize_fuel_type(out.get("Fuel Type"))

    return out


# Mirrors openpyxl.cell.cell.ILLEGAL_CHARACTERS_RE: ASCII control chars Excel rejects.
# Stripping these before write prevents IllegalCharacterError from aborting whole-workbook writes.
_OPENPYXL_ILLEGAL_CHARS_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")


def _sanitize_for_openpyxl(df: pd.DataFrame) -> pd.DataFrame:
    """Strip control chars from string cells so openpyxl won't raise IllegalCharacterError."""
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(
                lambda v: _OPENPYXL_ILLEGAL_CHARS_RE.sub("", v) if isinstance(v, str) else v
            )
    return out


def dataframe_with_excel_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with analyst-facing column labels for Excel."""
    out = df.copy()
    rename = {k: v for k, v in EXCEL_COLUMN_DISPLAY_NAMES.items() if k in out.columns}
    return out.rename(columns=rename)


def read_permit_excel_full_table(path) -> pd.DataFrame:
    """Load combined permit table from a single-sheet or two-sheet (NAICS split) workbook."""
    try:
        xl = pd.ExcelFile(path, engine="openpyxl")
    except Exception:
        return pd.DataFrame()

    names = xl.sheet_names
    if len(names) >= 2 and "Manufacturing NAICS 31-33" in names and "Other NAICS" in names:
        m = pd.read_excel(path, sheet_name="Manufacturing NAICS 31-33", engine="openpyxl")
        o = pd.read_excel(path, sheet_name="Other NAICS", engine="openpyxl")
        if m.empty and o.empty:
            return pd.DataFrame()
        return pd.concat([m, o], ignore_index=True)
    return pd.read_excel(path, engine="openpyxl")


def write_permit_excel_multisheet(df: pd.DataFrame, path, column_order: list):
    """Write two tabs: Manufacturing (NAICS 31–33) and all other rows; rows sorted by NAICS then Filename."""
    path = Path(path)
    df = df.copy()
    df = df.dropna(how="all")
    # Drop stray rows with neither filename nor facility name
    if "Filename" in df.columns:
        fn_blank = df["Filename"].isna() | (df["Filename"].astype(str).str.strip().isin(("", "nan")))
        if "Facility Name" in df.columns:
            fac_blank = df["Facility Name"].isna() | (
                df["Facility Name"].astype(str).str.strip().isin(("", "nan"))
            )
            df = df[~(fn_blank & fac_blank)]
        else:
            df = df[~fn_blank]

    for col in column_order:
        if col not in df.columns:
            df[col] = None
    classified_col = "Classified NAICS"
    df = df[column_order + ([classified_col] if classified_col in df.columns else [])]

    from permit_data_extraction.industry_description_classifier import (
        classify_industry_to_naics,
    )

    # The "Manufacturing" tab covers the four manufacturing-sector groupings:
    # NAICS 31/32/33 (traditional manufacturing) plus the three industrial-
    # process sectors that share emission-source characteristics:
    #   - Data Centers  (NAICS 518210)
    #   - Water         (NAICS 221310)
    #   - Wastewater    (NAICS 221320)
    extra_mfg_naics = {"518210", "221310", "221320"}
    extra_mfg_sic = {"7374", "4941", "4952"}

    def _code_is_mfg_tab(code) -> bool:
        if is_manufacturing_naics_3133(code):
            return True
        digits = re.sub(r"\D", "", str(code or ""))
        if not digits:
            return False
        return digits in extra_mfg_naics or digits in extra_mfg_sic

    desc_codes = (
        df["Industry Description"].apply(classify_industry_to_naics)
        if "Industry Description" in df.columns
        else pd.Series([None] * len(df), index=df.index)
    )
    df[classified_col] = desc_codes
    desc_is_mfg = desc_codes.apply(lambda c: bool(c) and _code_is_mfg_tab(c))
    desc_is_non_mfg = desc_codes.notna() & ~desc_is_mfg

    naics_is_mfg = df["NAICS Code"].apply(_code_is_mfg_tab)
    if "SIC Code" in df.columns:
        sic_is_mfg = df["SIC Code"].apply(_code_is_mfg_tab)
        code_is_mfg = naics_is_mfg | sic_is_mfg
    else:
        code_is_mfg = naics_is_mfg

    # Industry Description is primary; NAICS/SIC code is the fallback when
    # the description didn't match any rule.
    mfg_mask = desc_is_mfg | (~desc_is_non_mfg & code_is_mfg)
    mfg = df[mfg_mask].copy()
    other = df[~mfg_mask].copy()

    sort_cols = [c for c in ("NAICS Code", "Filename") if c in df.columns]
    if sort_cols:
        mfg = mfg.sort_values(by=sort_cols, na_position="last")
        other = other.sort_values(by=sort_cols, na_position="last")

    mfg_out = _sanitize_for_openpyxl(dataframe_with_excel_headers(mfg))
    other_out = _sanitize_for_openpyxl(dataframe_with_excel_headers(other))

    # Atomic write: write to a temp file in the same directory, then rename.
    # This prevents corruption if the process crashes mid-write.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".xlsx", dir=path.parent)
    os.close(tmp_fd)
    try:
        with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
            mfg_out.to_excel(writer, sheet_name="Manufacturing NAICS 31-33", index=False)
            other_out.to_excel(writer, sheet_name="Other NAICS", index=False)
        shutil.move(tmp_path, path)
    except BaseException:
        # Clean up the temp file if the write failed
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logging.info(
        "Wrote Excel with Manufacturing=%s rows, Other=%s rows -> %s",
        len(mfg),
        len(other),
        path,
    )


def setup_processing_tracking():
    """Ensure tracking log parent directory exists."""
    PROCESSING_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.info(f"Processing log path ready at: {PROCESSING_LOG_PATH}")


def _load_processing_status_map():
    """
    Load latest processing status per filename from JSONL log.
    Returns {filename: status} where status is "success" or "failed".
    """
    status_map = {}
    if not PROCESSING_LOG_PATH.exists():
        return status_map

    try:
        with open(PROCESSING_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    logging.warning("Skipping malformed log line in processing log.")
                    continue
                filename = entry.get("filename")
                status = str(entry.get("status", "")).strip().lower()
                if filename and status in {"success", "failed"}:
                    status_map[filename] = status
    except Exception as e:
        logging.error(f"Failed reading processing log: {e}", exc_info=True)

    return status_map


def log_file_processing_result(file_path, status, model_used=None, error=None):
    """Append one processing result to persistent JSONL tracking log."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "filename": file_path.name,
        "file_path": str(file_path),
        "status": status,
        "model_used": model_used,
    }
    if error:
        entry["error"] = str(error)[:500]
    with PROCESSING_LOG_LOCK:
        with open(PROCESSING_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def append_rows_to_excel(new_rows, llm_client=None):
    """
    Append new rows to the Excel file, creating it if it doesn't exist.
    Handles incremental saving to prevent data loss if script is interrupted.
    
    Args:
        new_rows (list): List of dictionaries, each representing a row to add
        llm_client: Optional LLM client for spec sheet lookup (not used in incremental saves)
    """
    if not new_rows:
        return
    
    try:
        # Add current date and model used to all new rows
        current_date = datetime.now().strftime("%Y-%m-%d")
        for row in new_rows:
            row["Processing Date"] = current_date
            row["Model Used"] = LLM_MODEL
        
        excel_columns = build_excel_column_order()
        
        # Ensure all columns exist in new rows
        for row in new_rows:
            for col in excel_columns:
                if col not in row:
                    row[col] = None
        
        # Create DataFrame from new rows
        new_df = pd.DataFrame(new_rows)
        new_df = new_df[excel_columns]
        
        # Check if Excel file exists
        if os.path.exists(OUTPUT_EXCEL_FILE):
            # Read existing file
            existing_df = load_existing_excel(excel_columns)
            if not existing_df.empty:
                # Remove any rows that match the new rows (by Filename) to avoid duplicates when retrying
                # This handles the case where we're retrying failed files
                filenames_to_update = set(new_df['Filename'].unique())
                existing_df = existing_df[~existing_df['Filename'].isin(filenames_to_update)]
            
            # Combine existing and new data
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)
        else:
            # Create new file
            combined_df = new_df
        
        write_permit_excel_multisheet(combined_df, OUTPUT_EXCEL_FILE, excel_columns)
        logging.info(f"  Appended {len(new_rows)} row(s) to Excel file: {OUTPUT_EXCEL_FILE}")
        
    except Exception as e:
        logging.error(f"Error appending rows to Excel: {e}", exc_info=True)
        print(f"  Warning: Failed to save rows to Excel: {e}")


def load_existing_excel(excel_columns):
    """Load existing Excel data once to avoid repeated IO."""
    if os.path.exists(OUTPUT_EXCEL_FILE):
        try:
            existing_df = read_permit_excel_full_table(OUTPUT_EXCEL_FILE)
        except Exception as e:
            logging.error(f"Failed to read Excel file: {e}", exc_info=True)
            print(f"Warning: Failed to read Excel file, creating a new one: {e}")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = OUTPUT_EXCEL_FILE.replace(".xlsx", f"_corrupt_{timestamp}.xlsx")
            try:
                shutil.move(OUTPUT_EXCEL_FILE, backup_file)
                logging.warning(f"Moved unreadable Excel file to: {backup_file}")
                print(f"  Moved unreadable Excel file to: {backup_file}")
            except Exception as move_error:
                logging.error(
                    f"Failed to move unreadable Excel file: {move_error}",
                    exc_info=True,
                )
                print(f"  Warning: Failed to move unreadable Excel file: {move_error}")
            return pd.DataFrame(columns=excel_columns)

        for col in excel_columns:
            if col not in existing_df.columns:
                existing_df[col] = None
        return existing_df
    return pd.DataFrame(columns=excel_columns)


def combine_run_into_combined(run_output_path, excel_columns):
    """
    Merge this run's Excel file into the main combined file (OUTPUT_EXCEL_FILE).
    Combined file = existing combined data + this run's data.
    """
    if not run_output_path.exists():
        logging.warning(f"Run output file not found: {run_output_path}; skipping combine.")
        return
    try:
        run_df = read_permit_excel_full_table(run_output_path)
        if run_df.empty:
            logging.info("Run output is empty; nothing to combine.")
            return
        for col in excel_columns:
            if col not in run_df.columns:
                run_df[col] = None
        run_df = run_df[excel_columns]

        if os.path.exists(OUTPUT_EXCEL_FILE):
            existing_df = read_permit_excel_full_table(OUTPUT_EXCEL_FILE)
            for col in excel_columns:
                if col not in existing_df.columns:
                    existing_df[col] = None
            existing_df = existing_df[excel_columns]
            combined_df = pd.concat([existing_df, run_df], ignore_index=True)
        else:
            combined_df = run_df

        write_permit_excel_multisheet(combined_df, OUTPUT_EXCEL_FILE, excel_columns)
        logging.info(f"Combined run output into {OUTPUT_EXCEL_FILE} (run file: {run_output_path.name})")
        print(f"  Combined run into {OUTPUT_EXCEL_FILE}")
    except Exception as e:
        logging.error(f"Error combining run into Excel: {e}", exc_info=True)
        print(f"  Warning: Failed to combine run into Excel: {e}")


def _get_latest_excel_output_file():
    processed_dir = Path(PROCESSED_DATA_DIR)
    if not processed_dir.exists():
        return None

    candidates = sorted(processed_dir.glob("permit_data_extracted*.xlsx"))
    if not candidates:
        candidates = sorted(processed_dir.glob("*.xlsx"))
    if not candidates:
        return None

    return max(candidates, key=lambda p: p.stat().st_mtime)


def _normalize_facility_key(row):
    parts = [
        str(row.get("Facility Name", "")).strip().lower(),
        str(row.get("Facility Address", "")).strip().lower(),
        str(row.get("Facility City", "")).strip().lower(),
        str(row.get("Facility State Abbreviation", "")).strip().lower(),
        str(row.get("Facility Zip Code", "")).strip().lower(),
    ]
    if any(parts):
        return "|".join(parts)
    filename = str(row.get("Filename", "")).strip().lower()
    return f"unknown::{filename}"


def _cell_has_equipment_value(value):
    if pd.isna(value):
        return False
    text = str(value).strip()
    if not text:
        return False
    if text.upper() in {"ERROR", "INVALID UNIT ENTRY"}:
        return False
    return True


def clean_latest_excel_output():
    """
    Keep only successful rows, dedupe by facility (newest processing date wins), and add:
    - **Duplicate Equipment Documents:** count of distinct source PDF filenames that had equipment
      rows for the same facility key (helps spot multiple permit files per site).
    - **Latest Facility Filename:** the PDF filename kept after deduplication (newest run).
    Writes a two-sheet workbook (Manufacturing NAICS 31–33 vs Other) with post-processing applied.
    """
    latest_file = _get_latest_excel_output_file()
    if not latest_file:
        print("No Excel files found to clean.")
        logging.info("No Excel files found for cleaning.")
        return

    try:
        df = read_permit_excel_full_table(latest_file)
    except Exception as e:
        logging.error(f"Failed to read Excel file for cleaning: {e}", exc_info=True)
        print(f"ERROR: Failed to read Excel file for cleaning: {e}")
        return

    if df.empty:
        print(f"Excel file '{latest_file.name}' is empty; skipping cleaning.")
        logging.info("Excel file is empty; skipping cleaning.")
        return

    original_row_count = len(df)
    removed_failures = 0
    removed_duplicates = 0

    if "Status" in df.columns:
        status_series = df["Status"].fillna("").astype(str).str.lower()
        success_mask = status_series.str.startswith("success")
        removed_failures = int((~success_mask).sum())
        df = df[success_mask].copy()

    if df.empty:
        print("All rows were filtered out as failures; nothing to deduplicate.")
        logging.info("Cleaning removed all rows as failures.")
        return

    if "Processing Date" in df.columns:
        processing_dates = pd.to_datetime(df["Processing Date"], errors="coerce")
    else:
        processing_dates = pd.Series([pd.NaT] * len(df), index=df.index)

    if processing_dates.isna().all():
        file_mtime = datetime.fromtimestamp(latest_file.stat().st_mtime)
        processing_dates = pd.Series([file_mtime] * len(df), index=df.index)

    df["Facility Key"] = df.apply(_normalize_facility_key, axis=1)

    equipment_fields = [field for field in UNIT_DETAIL_FIELDS if field in df.columns]
    if equipment_fields:
        equipment_mask = df[equipment_fields].applymap(_cell_has_equipment_value).any(axis=1)
    else:
        equipment_mask = pd.Series([False] * len(df), index=df.index)

    equipment_docs = df.loc[equipment_mask, ["Facility Key", "Filename"]]
    equipment_doc_counts = (
        equipment_docs.groupby("Facility Key")["Filename"].nunique()
        if not equipment_docs.empty
        else pd.Series(dtype=int)
    )

    df["Duplicate Equipment Documents"] = df["Facility Key"].map(equipment_doc_counts).fillna(0).astype(int)

    df["__processing_date"] = processing_dates
    latest_filename_by_facility = {}
    for facility_key, facility_rows in df.groupby("Facility Key"):
        max_date = facility_rows["__processing_date"].max()
        latest_rows = facility_rows[facility_rows["__processing_date"] == max_date]
        latest_filename = latest_rows["Filename"].dropna().astype(str).max()
        latest_filename_by_facility[facility_key] = latest_filename

    df["Latest Facility Filename"] = df["Facility Key"].map(latest_filename_by_facility)
    keep_mask = df["Filename"].astype(str) == df["Latest Facility Filename"].astype(str)
    removed_duplicates = int((~keep_mask).sum())
    df = df[keep_mask].copy()

    df.drop(columns=["Facility Key", "__processing_date"], inplace=True, errors="ignore")

    # Re-apply normalization for legacy rows (dates, zips, hours split, fuel synonyms)
    records = df.to_dict("records")
    cleaned_records = []
    for r in records:
        if str(r.get("Status", "")).startswith("Success"):
            cleaned_records.append(postprocess_extraction_row(r))
        else:
            cleaned_records.append(r)
    df = pd.DataFrame(cleaned_records)

    excel_columns = build_excel_column_order()
    for col in excel_columns:
        if col not in df.columns:
            df[col] = None
    df = df[excel_columns]

    backup_file = latest_file.with_name(latest_file.stem + "_pre_cleaning_backup.xlsx")
    try:
        shutil.copy2(latest_file, backup_file)
        logging.info(f"Created pre-cleaning backup: {backup_file}")
    except Exception as e:
        logging.warning(f"Failed to create backup before cleaning: {e}")

    try:
        write_permit_excel_multisheet(df, latest_file, excel_columns)
        print(f"Cleaned Excel file saved: {latest_file}")
        print(f"  - Removed failures: {removed_failures}")
        print(f"  - Removed duplicate rows: {removed_duplicates}")
        print(f"  - Rows before: {original_row_count}, after: {len(df)}")
        logging.info(
            "Cleaned Excel file saved: %s (removed failures=%s, removed duplicates=%s, before=%s, after=%s)",
            latest_file,
            removed_failures,
            removed_duplicates,
            original_row_count,
            len(df),
        )
    except Exception as e:
        logging.error(f"Failed to write cleaned Excel file: {e}", exc_info=True)
        print(f"ERROR: Failed to write cleaned Excel file: {e}")


def merge_rows(existing_df, new_rows, excel_columns):
    """Merge new rows into existing dataframe, de-duplicating by Filename."""
    if not new_rows:
        return existing_df

    # Add current date and model used to all new rows
    current_date = datetime.now().strftime("%Y-%m-%d")
    for row in new_rows:
        row["Processing Date"] = current_date
        if "Model Used" not in row:
            row["Model Used"] = LLM_MODEL

    # Ensure all columns exist in new rows
    for row in new_rows:
        for col in excel_columns:
            if col not in row:
                row[col] = None

    new_df = pd.DataFrame(new_rows)
    new_df = new_df[excel_columns]

    if existing_df is None or existing_df.empty:
        return new_df

    filenames_to_update = set(new_df['Filename'].unique())
    existing_df = existing_df[~existing_df['Filename'].isin(filenames_to_update)]
    return pd.concat([existing_df, new_df], ignore_index=True)


def configure_llm():
    """Configures the OpenAI client."""
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY,
                               base_url="https://api.cborg.lbl.gov")
        # Test the connection with a simple call
        client.models.list()
        print("OpenAI client configured successfully.")
        print(f"  Primary model: {LLM_MODEL}")
        print(f"  Fallback model: {LLM_LARGE_MODEL}")
        return client
    except Exception as e:
        print(f"Error configuring OpenAI client: {e}")
        print("Please ensure your OpenAI API key is correct and valid.")
        return None


def setup_selenium_driver():
    """Setup and configure a WebDriver for Selenium (Chrome preferred, Firefox fallback)."""
    
    # Try Chrome first
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')  # Run in background
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
        
        driver = webdriver.Chrome(options=chrome_options)
        logging.info("Successfully setup Chrome WebDriver")
        return driver
    except Exception as e:
        logging.warning(f"Failed to setup Chrome WebDriver: {e}")
    
    # Try Firefox as fallback
    try:
        from selenium.webdriver.firefox.options import Options as FirefoxOptions
        firefox_options = FirefoxOptions()
        firefox_options.add_argument('--headless')
        firefox_options.add_argument('--width=1920')
        firefox_options.add_argument('--height=1080')
        
        driver = webdriver.Firefox(options=firefox_options)
        logging.info("Successfully setup Firefox WebDriver")
        return driver
    except Exception as e:
        logging.warning(f"Failed to setup Firefox WebDriver: {e}")
    
    # Try Chromium as another fallback
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.binary_location = '/usr/bin/chromium-browser'  # Common path for Chromium
        
        driver = webdriver.Chrome(options=chrome_options)
        logging.info("Successfully setup Chromium WebDriver")
        return driver
    except Exception as e:
        logging.warning(f"Failed to setup Chromium WebDriver: {e}")
    
    logging.error("Failed to setup any WebDriver (Chrome, Firefox, or Chromium)")
    return None


def perform_google_search_selenium(search_query, max_results=10):
    """
    Perform a Google search using Selenium and return the top search results.
    
    Args:
        search_query (str): The search query
        max_results (int): Maximum number of results to return
        
    Returns:
        list: List of dictionaries with 'title', 'url', and 'snippet' keys
    """
    driver = None
    try:
        # Setup Chrome driver
        driver = setup_selenium_driver()
        if not driver:
            logging.error("Failed to setup WebDriver")
            return []
        
        # Create a Google search URL
        encoded_query = quote_plus(search_query)
        google_url = f"https://www.google.com/search?q={encoded_query}"
        
        # Navigate to Google
        driver.get(google_url)
        
        # Wait for search results to load
        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.g")))
        
        # Extract search results
        search_results = []
        
        # Find search result containers
        result_elements = driver.find_elements(By.CSS_SELECTOR, "div.g")[:max_results]
        
        for element in result_elements:
            try:
                # Extract title
                title_elem = element.find_element(By.CSS_SELECTOR, "h3")
                title = title_elem.text.strip() if title_elem else ""
                
                # Extract link
                link_elem = element.find_element(By.CSS_SELECTOR, "a")
                url = link_elem.get_attribute('href') if link_elem else ""
                
                # Extract snippet
                snippet = ""
                try:
                    snippet_elem = element.find_element(By.CSS_SELECTOR, "span.st, div.VwiC3b")
                    snippet = snippet_elem.text.strip() if snippet_elem else ""
                except NoSuchElementException:
                    # Try alternative selectors for snippets
                    try:
                        snippet_elem = element.find_element(By.CSS_SELECTOR, "div.s3v9rd")
                        snippet = snippet_elem.text.strip() if snippet_elem else ""
                    except NoSuchElementException:
                        pass
                
                if title and url:
                    search_results.append({
                        'title': title,
                        'url': url,
                        'snippet': snippet
                    })
                    
            except Exception as e:
                logging.warning(f"Error parsing search result: {e}")
                continue
        
        return search_results
        
    except TimeoutException:
        logging.warning(f"Timeout waiting for Google search results for '{search_query}'")
        return []
    except Exception as e:
        logging.warning(f"Error performing Google search with Selenium for '{search_query}': {e}")
        return []
    finally:
        if driver:
            driver.quit()


def search_manufacturer_direct(make, model):
    """Search for spec sheets by constructing direct manufacturer URLs."""
    manufacturer_urls = {
        "caterpillar": "https://www.cat.com/en_US/products/new/power-systems/industrial-engines/",
        "ge": "https://www.ge.com/gas-power/products/gas-turbines/",
        "cleaver brooks": "https://www.cleaverbrooks.com/products/boilers/",
        "john deere": "https://www.deere.com/en/engines/",
        "cummins": "https://www.cummins.com/engines/",
        "detroit diesel": "https://www.detroitdiesel.com/engines/",
        "perkins": "https://www.perkins.com/en_US/products/engines/",
        "kohler": "https://www.kohlerpower.com/engines/",
        "briggs & stratton": "https://www.briggsandstratton.com/engines/",
        "honda": "https://engines.honda.com/",
        "yanmar": "https://www.yanmar.com/us/engines/",
        "kubota": "https://www.kubota.com/products/engines/",
        "isuzu": "https://www.isuzuengines.com/",
        "mitsubishi": "https://www.mitsubishi-engines.com/",
        "volvo": "https://www.volvopenta.com/en-us/engines/",
        "man": "https://www.man.eu/en/engines/",
        "deutz": "https://www.deutz.com/en/products/engines/",
        "mtu": "https://www.mtu-online.com/engines/",
        "rolls-royce": "https://www.rolls-royce.com/products-and-services/marine/engines/",
        "wärtsilä": "https://www.wartsila.com/energy/engines"
    }
    
    make_lower = make.lower().strip()
    
    if make_lower in manufacturer_urls:
        base_url = manufacturer_urls[make_lower]
        return [{
            "title": f"{make} {model} - Official Product Page",
            "url": base_url,
            "snippet": f"Official {make} product page where you can find specifications for {model}"
        }]
    
    return []


def perform_google_search(search_query, max_results=10):
    """
    Perform a search using multiple methods: Selenium, requests, and manufacturer direct.
    
    Args:
        search_query (str): The search query
        max_results (int): Maximum number of results to return
        
    Returns:
        list: List of dictionaries with 'title', 'url', and 'snippet' keys
    """
    # Try Selenium first
    results = perform_google_search_selenium(search_query, max_results)
    if results:
        logging.info(f"Selenium search successful for '{search_query}'")
        return results
    
    # Fallback to requests if Selenium fails
    logging.info("Selenium search failed, trying requests fallback...")
    try:
        # Create a Google search URL
        encoded_query = quote_plus(search_query)
        google_url = f"https://www.google.com/search?q={encoded_query}"
        
        # Set headers to mimic a real browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Perform the search
        response = requests.get(google_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Simple parsing for fallback (basic regex-based extraction)
        search_results = []
        content = response.text
        
        # Extract URLs and titles using regex (basic fallback)
        url_pattern = r'href="(/url\?q=|https://)([^"]+)"'
        title_pattern = r'<h3[^>]*>([^<]+)</h3>'
        
        urls = re.findall(url_pattern, content)[:max_results]
        titles = re.findall(title_pattern, content)[:max_results]
        
        for i, (url_prefix, url) in enumerate(urls):
            if i < len(titles):
                # Clean up the URL
                if url_prefix == '/url?q=':
                    url = url.split('&')[0]
                else:
                    url = url_prefix + url
                
                search_results.append({
                    'title': titles[i].strip(),
                    'url': url,
                    'snippet': ''  # Snippet extraction is complex, skip for fallback
                })
        
        if search_results:
            logging.info(f"Requests fallback successful for '{search_query}'")
            return search_results
        
    except Exception as e:
        logging.warning(f"Fallback requests search also failed for '{search_query}': {e}")
    
    # Final fallback: try to extract make/model from query and use manufacturer direct
    logging.info("All search methods failed, trying manufacturer direct search...")
    try:
        # Extract make and model from the search query
        # Look for quoted terms in the search query
        import re
        quoted_terms = re.findall(r'"([^"]+)"', search_query)
        if len(quoted_terms) >= 2:
            make = quoted_terms[0]
            model = quoted_terms[1]
            manufacturer_results = search_manufacturer_direct(make, model)
            if manufacturer_results:
                logging.info(f"Found manufacturer direct results for {make} {model}")
                return manufacturer_results
    except Exception as e:
        logging.warning(f"Manufacturer direct search failed: {e}")
    
    logging.warning(f"All search methods failed for '{search_query}'")
    return []


def analyze_search_results_with_llm(make, model, search_results, client):
    """
    Use LLM to analyze search results and identify the best spec sheet links.
    
    Args:
        make (str): Equipment manufacturer/make
        model (str): Equipment model
        search_results (list): List of search result dictionaries
        client: OpenAI client instance
        
    Returns:
        list: List of URLs identified as spec sheets/manuals
    """
    if not search_results or not client:
        return []
    
    # Prepare the search results for LLM analysis
    results_text = ""
    for i, result in enumerate(search_results, 1):
        results_text += f"{i}. Title: {result['title']}\n"
        results_text += f"   URL: {result['url']}\n"
        results_text += f"   Snippet: {result['snippet']}\n\n"
    
    prompt = f"""
You are an expert at identifying equipment specification sheets and technical manuals from search results.

Equipment: {make} {model}

Search Results:
{results_text}

Please analyze these search results and identify which URLs are most likely to be:
1. Official equipment specification sheets
2. Technical manuals
3. Datasheets
4. Product documentation

Return your analysis as a JSON object with this structure:
{{
    "spec_sheets": [
        {{
            "url": "URL_HERE",
            "title": "TITLE_HERE",
            "confidence": "high|medium|low",
            "reason": "Why this is likely a spec sheet"
        }}
    ]
}}

Only include URLs with "high" or "medium" confidence. If there are multiple good matches, include up to 2 best ones.
Focus on official manufacturer websites, technical documentation sites, and direct PDF links.
"""
    
    try:
        response = client.chat.completions.create(
            model="lbl/llama",
            messages=[
                {"role": "system", "content": "You are an expert at identifying technical documentation from search results. Always respond with valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=2048,
            timeout=30,
            response_format={"type": "json_object"}
        )
        
        if response and response.choices and len(response.choices) > 0:
            json_text = response.choices[0].message.content.strip()
            
            # Clean up JSON response
            if '```json' in json_text:
                json_text = json_text.split('```json')[1].split('```')[0].strip()
            elif '```' in json_text:
                json_text = json_text.split('```')[1].split('```')[0].strip()
            
            analysis = json.loads(json_text)
            spec_sheets = analysis.get('spec_sheets', [])
            
            # Return only high and medium confidence results
            filtered_results = [sheet for sheet in spec_sheets if sheet.get('confidence') in ['high', 'medium']]
            return filtered_results
            
    except Exception as e:
        logging.warning(f"Error analyzing search results with LLM for {make} {model}: {e}")
    
    return []


def search_spec_sheet(make, model, client=None):
    """
    Search for equipment specification sheets based on make and model using Google search and LLM analysis.
    
    Args:
        make (str): Equipment manufacturer/make
        model (str): Equipment model
        client: OpenAI client instance for LLM analysis
        
    Returns:
        str: URLs to spec sheets (comma-separated if multiple), empty string if not found
    """
    if not make or not model or make.strip() == "" or model.strip() == "":
        return ""
    
    # Clean the make and model for better search results
    clean_make = make.strip()
    clean_model = model.strip()
    
    # Common equipment types that might have spec sheets
    equipment_keywords = ["boiler", "furnace", "engine", "turbine", "compressor", "generator", "motor"]
    
    # Check if this looks like industrial equipment
    is_equipment = any(keyword in clean_model.lower() or keyword in clean_make.lower() for keyword in equipment_keywords)
    
    if not is_equipment:
        # For non-equipment items, try a general search
        search_query = f'"{clean_make}" "{clean_model}" specifications manual datasheet'
    else:
        # For equipment, use more specific search terms
        search_query = f'"{clean_make}" "{clean_model}" specification sheet manual datasheet technical documentation'
    
    logging.info(f"Searching for spec sheets: {make} {model}")
    
    # Perform Google search
    search_results = perform_google_search(search_query, max_results=10)
    
    if not search_results:
        logging.info(f"No search results found for {make} {model}")
        return ""
    
    logging.info(f"Found {len(search_results)} search results for {make} {model}")
    
    # Analyze results with LLM if client is available
    if client:
        spec_sheets = analyze_search_results_with_llm(make, model, search_results, client)
        
        if spec_sheets:
            # Return up to 2 best matches, comma-separated
            urls = [sheet['url'] for sheet in spec_sheets[:2]]
            logging.info(f"LLM identified {len(urls)} spec sheet(s) for {make} {model}")
            return ", ".join(urls)
    
    # Fallback: return first result if LLM analysis failed
    logging.info(f"LLM analysis failed for {make} {model}, returning first result as fallback")
    return search_results[0]['url'] if search_results else ""


def add_spec_sheet_links(df, llm_client=None):
    """
    Add spec sheet links to a dataframe that has Unit Make and Unit Model columns.
    
    Args:
        df (pd.DataFrame): DataFrame with equipment data
        llm_client: OpenAI client instance for LLM analysis
        
    Returns:
        pd.DataFrame: DataFrame with added Spec Sheet Link column
    """
    if df.empty:
        return df
    
    # Initialize the spec sheet link column
    df['Spec Sheet Link'] = ""

    if not ENABLE_SPEC_SHEET_LOOKUP:
        logging.info("Spec sheet lookup disabled by configuration. Skipping search.")
        return df
    
    # Find rows that have both make and model
    has_make_model = df['Unit Make'].notna() & df['Unit Model'].notna() & (df['Unit Make'] != "") & (df['Unit Model'] != "")
    
    if has_make_model.any():
        logging.info(f"Searching for spec sheets for {has_make_model.sum()} equipment items...")
        
        for idx in df[has_make_model].index:
            make = df.loc[idx, 'Unit Make']
            model = df.loc[idx, 'Unit Model']
            
            if make and model:
                spec_link = search_spec_sheet(make, model, llm_client)
                df.loc[idx, 'Spec Sheet Link'] = spec_link
                
                if spec_link:
                    logging.info(f"  Found spec sheet link(s) for {make} {model}: {spec_link}")
                else:
                    logging.info(f"  No spec sheet found for {make} {model}")
                
                # Add a small delay to avoid overwhelming the search APIs
                time.sleep(1)
    
    return df


def extract_text_from_pdf(pdf_path):
    """Extracts text from a text-based PDF file."""
    text = ""
    try:
        with open(pdf_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            num_pages = len(reader.pages)
            print(f"  Reading {num_pages} pages from {pdf_path.name}...")
            for page_num in range(num_pages):
                page = reader.pages[page_num]
                text += page.extract_text() or ""  # fallback for blank pages
            print(f"  Successfully extracted text from {pdf_path.name}.")
        return text
    except FileNotFoundError:
        print(f"  Error: File not found at {pdf_path}")
        return None
    except Exception as e:
        print(f"  Error reading PDF {pdf_path.name}: {e}")
        return None


def read_text_from_file(file_path):
    """Reads text content from a given file path."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        logging.info(f"  Successfully read text from {file_path.name} (approx {len(content)} chars).")
        return content
    except FileNotFoundError:
        logging.error(f"  Error: Text file not found at {file_path}")
        return None
    except Exception as e:
        logging.error(f"  Error reading text file {file_path.name}: {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Chunking: split long documents so smaller-context models can process them
# ---------------------------------------------------------------------------

# Rough chars-per-token ratio for English text; used to estimate prompt overhead.
_CHARS_PER_TOKEN = 4

# Target the 131,072-token context of GPT-oss-120B with a 32,768-token output budget:
# 131,072 total − 32,768 completion − ~2,537 prompt template − ~2,000 safety margin
# ≈ 93,767 tokens for text. At ~3.5 chars/token conservative, that's ~328k chars.
DEFAULT_MAX_CHUNK_CHARS = 320_000
CHUNK_OVERLAP_CHARS = 4_000  # overlap to avoid splitting a unit description in half


def _estimate_prompt_overhead_chars() -> int:
    """Character count of the prompt template minus the {permit_text} placeholder."""
    return len(PROMPT_TEMPLATE) - len("{permit_text}")


def _split_text_into_chunks(text: str, max_chunk_chars: int) -> list[str]:
    """Split *text* into chunks of at most *max_chunk_chars* characters.

    Splitting prefers paragraph boundaries (double-newline) so we avoid cutting
    mid-sentence.  Each chunk (except the first) starts with *CHUNK_OVERLAP_CHARS*
    characters of the previous chunk's tail so context around section boundaries
    is preserved.

    Returns a list with at least one element.
    """
    if len(text) <= max_chunk_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + max_chunk_chars

        if end >= len(text):
            chunks.append(text[start:])
            break

        # Try to split at a paragraph boundary (double newline) within the last
        # 20% of the chunk so we don't create tiny fragments.
        search_start = start + int(max_chunk_chars * 0.8)
        split_pos = text.rfind("\n\n", search_start, end)
        if split_pos == -1:
            # Fall back to single newline
            split_pos = text.rfind("\n", search_start, end)
        if split_pos == -1:
            # Fall back to space
            split_pos = text.rfind(" ", search_start, end)
        if split_pos == -1:
            split_pos = end  # hard cut

        chunk_end = split_pos
        chunks.append(text[start:chunk_end])

        # Next chunk starts with overlap from the tail of the current chunk
        start = max(chunk_end - CHUNK_OVERLAP_CHARS, start + 1)

    return chunks


def _merge_chunk_extractions(chunk_results: list[dict]) -> dict:
    """Merge extraction dicts from multiple chunks of the same document.

    Strategy:
    - General fields: prefer the first chunk that provides a non-null value
      (the document header is usually in chunk 0).
    - Emission Units: concatenate all units, then deduplicate by Unit ID
      (keeping the first occurrence with the most populated fields).
    """
    merged: dict = {}

    # --- General fields ---
    for field in GENERAL_TARGET_FIELDS:
        for cr in chunk_results:
            val = cr.get(field)
            if val is not None:
                merged[field] = val
                break
        else:
            merged[field] = None

    # --- Emission Units ---
    all_units: list[dict] = []
    for cr in chunk_results:
        units = cr.get("Emission Units") or []
        if isinstance(units, list):
            all_units.extend(u for u in units if isinstance(u, dict))

    # Deduplicate by _unit_key; keep the version with the most non-null fields
    seen: dict[str, dict] = {}
    for u in all_units:
        key = _unit_key(u)
        existing = seen.get(key)
        if existing is None:
            seen[key] = u
        else:
            new_count = sum(1 for v in u.values() if v is not None)
            old_count = sum(1 for v in existing.values() if v is not None)
            if new_count > old_count:
                seen[key] = u
    merged["Emission Units"] = list(seen.values())

    return merged


def _is_token_limit_error(error):
    """Heuristics for identifying token/context limit errors.
    Accepts an Exception or a plain error string."""
    message = str(error).lower()
    result = (
        "maximum context length" in message
        or "context length" in message
        or "context window" in message
        or "token limit" in message
        or "too many tokens" in message
        or ("context" in message and "token" in message)
        or "max_tokens must be at least 1" in message  # CBORG proxy: input exceeds context window
        or ("max_tokens" in message and "got -" in message)  # CBORG proxy: negative max_tokens
        or "finish_reason=length" in message  # output truncated at provider's max output tokens
    )
    if not result:
        logging.info(f"  _is_token_limit_error=False for: {message[:300]}")
    return result


LLM_MAX_RETRIES = 5  # Max retries on rate-limit / transient errors
LLM_BACKOFF_BASE = 2.0  # Exponential backoff base (seconds)
LLM_BACKOFF_MAX = 60.0  # Cap on backoff delay


def _is_retryable_error(error):
    """Check if an API error is retryable (rate-limit or transient server error)."""
    error_str = str(error).lower()
    # OpenAI SDK raises specific exception types; also check status codes in message
    if hasattr(error, "status_code"):
        if error.status_code in (429, 500, 502, 503, 504):
            return True
    return (
        "rate limit" in error_str
        or "429" in error_str
        or "502" in error_str
        or "503" in error_str
        or "server error" in error_str
        or "overloaded" in error_str
    )


def _invoke_llm_for_model(client, prompt, filename, model_name):
    """
    Call the LLM with the given model name and parse JSON response.
    Returns (extracted_data, error_str). On success error_str is None; on failure extracted_data is None.
    Retries with exponential backoff on rate-limit (429) and transient server errors.
    """
    time.sleep(0.1)  # Small stagger to avoid thundering herd
    logging.info(f"  Making API call for {filename} using model {model_name}...")

    last_error = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "You are an expert at extracting structured information from industrial air permit documents. Always respond with valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=32768,
                timeout=60,
                response_format={"type": "json_object"}
            )
            break  # Success — exit retry loop
        except Exception as e:
            last_error = e
            if _is_retryable_error(e) and attempt < LLM_MAX_RETRIES:
                delay = min(LLM_BACKOFF_BASE ** attempt + random.uniform(0, 1), LLM_BACKOFF_MAX)
                logging.warning(
                    f"  Retryable error for {filename} (attempt {attempt}/{LLM_MAX_RETRIES}): {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue
            logging.error(f"  Error during LLM API call for {filename} using {model_name}: {e}", exc_info=True)
            return None, str(e)

    logging.info(f"  API call completed for {filename} using model {model_name}")
    if not response or not response.choices or len(response.choices) == 0:
        logging.error(f"  LLM response was empty or malformed for {filename}.")
        return None, "Empty or malformed response"

    choice = response.choices[0]
    content = getattr(choice.message, "content", None)
    finish_reason = getattr(choice, "finish_reason", "unknown")
    if content is None:
        logging.warning(f"  LLM returned no content for {filename} (finish_reason={finish_reason}).")
        return None, f"Empty content (finish_reason={finish_reason})"

    # finish_reason="length" means the model hit the output-token cap and the JSON is
    # truncated mid-emission. Surface as a token-limit error so the chunking path picks it up.
    if finish_reason == "length":
        logging.warning(
            f"  Output truncated for {filename} (finish_reason=length, {len(content)} chars). "
            f"Will route to chunking."
        )
        return None, "Output truncated (finish_reason=length)"

    try:
        json_text_response = content.strip()
        if "```json" in json_text_response:
            json_text_response = json_text_response.split("```json")[1].split("```")[0].strip()
        elif "```" in json_text_response:
            json_text_response = json_text_response.split("```")[1].split("```")[0].strip()

        extracted_data = json.loads(json_text_response)
        if "Emission Units" not in extracted_data or not isinstance(extracted_data.get("Emission Units"), list):
            logging.warning(f"  LLM response for {filename} parsed, but 'Emission Units' key is missing or not a list.")
            extracted_data["Emission Units"] = []
        logging.info(f"  Successfully extracted and parsed JSON from {filename}.")
        return extracted_data, None
    except json.JSONDecodeError as json_e:
        logging.error(f"  Failed to decode JSON for {filename}. Error: {json_e}")
        return None, f"JSON decode error: {json_e}"
    except Exception as e:
        logging.error(f"  Error processing response for {filename}: {e}", exc_info=True)
        return None, str(e)


def extract_info_with_model(client, text_content, filename, model_name):
    """
    Run extraction for a single model. Returns (parsed_json_dict, error_string).
    Either result is not None or error is not None. Used by validation and by main pipeline.
    """
    if not client or not text_content:
        return None, "Missing client or text content"
    try:
        # Use replace, not str.format — the template embeds JSON with `{`/`}` braces.
        prompt = PROMPT_TEMPLATE.replace("{permit_text}", text_content)
    except Exception as format_e:
        return None, f"Prompt formatting error: {format_e}"
    logging.info(f"  Sending text from {filename} to LLM (approx {len(text_content)} chars)...")
    extracted_data, error_str = _invoke_llm_for_model(client, prompt, filename, model_name)
    return extracted_data, error_str


def _extract_chunked(client, text_content, filename, model_name, max_chunk_chars):
    """Split *text_content* into chunks, extract from each, and merge results.

    Returns (merged_dict | None, model_name_used).
    """
    chunks = _split_text_into_chunks(text_content, max_chunk_chars)
    logging.info(
        f"  Chunked extraction for {filename}: {len(chunks)} chunk(s) "
        f"(max_chunk_chars={max_chunk_chars}, doc={len(text_content)} chars)"
    )

    chunk_results: list[dict] = []
    last_error: str | None = None
    for i, chunk in enumerate(chunks, 1):
        logging.info(f"  Chunk {i}/{len(chunks)} for {filename} ({len(chunk)} chars)")
        data, err = extract_info_with_model(client, chunk, f"{filename}_chunk{i}", model_name)
        if data is not None:
            chunk_results.append(data)
        else:
            last_error = err
            logging.warning(f"  Chunk {i}/{len(chunks)} failed for {filename}: {err}")

    if not chunk_results:
        return None, last_error

    merged = _merge_chunk_extractions(chunk_results)
    logging.info(
        f"  Merged {len(chunk_results)}/{len(chunks)} chunk(s) for {filename}: "
        f"{len(merged.get('Emission Units', []))} emission unit(s)"
    )
    return merged, None


def extract_info_with_llm(
    client, text_content, filename, *,
    allow_large_model_retry=False,
    max_chunk_chars: int | None = DEFAULT_MAX_CHUNK_CHARS,
):
    """Sends text to the LLM and attempts to parse the JSON response.

    Extraction cascade:
    1. Try the primary model (LLM_MODEL) with the full document.
    2. On token-limit error, split the document into chunks and
       extract/merge with the same small model.
    3. If chunking also fails, fall back to LLM_LARGE_MODEL
       (when *allow_large_model_retry* is True).
    """
    if not client or not text_content:
        logging.warning(f"  Skipping LLM call for {filename} due to missing client or text.")
        return None, None

    # --- 1. Try full-document extraction with the primary model ---
    extracted_data, error_str = extract_info_with_model(client, text_content, filename, LLM_MODEL)
    if extracted_data is not None:
        return extracted_data, LLM_MODEL

    # --- 2. Chunked extraction (same small model) ---
    if error_str and _is_token_limit_error(error_str):
        chunk_size = max_chunk_chars or DEFAULT_MAX_CHUNK_CHARS
        logging.warning(
            f"  Token limit hit for {filename} using {LLM_MODEL}. "
            f"Splitting into chunks (max {chunk_size} chars each)."
        )
        extracted_data, chunk_err = _extract_chunked(
            client, text_content, filename, LLM_MODEL, chunk_size,
        )
        if extracted_data is not None:
            return extracted_data, f"{LLM_MODEL} (chunked)"
        logging.warning(f"  Chunked extraction also failed for {filename}: {chunk_err}")

    # --- 3. Large-model fallback ---
    if error_str and allow_large_model_retry and LLM_LARGE_MODEL != LLM_MODEL and _is_token_limit_error(error_str):
        logging.warning(f"  Token limit exceeded for {filename} using {LLM_MODEL}. Retrying with {LLM_LARGE_MODEL}.")
        extracted_data, _ = extract_info_with_model(client, text_content, filename, LLM_LARGE_MODEL)
        if extracted_data is not None:
            return extracted_data, LLM_LARGE_MODEL
        logging.error(f"  Error during retry with {LLM_LARGE_MODEL} for {filename}.", exc_info=True)
        return None, LLM_LARGE_MODEL
    if "API key not valid" in (error_str or ""):
        logging.error("  Hint: Double-check your OPENAI_API_KEY setting.")
    return None, LLM_MODEL


def sample_completed_permits(completed_dir, sample_size):
    """
    Randomly sample up to sample_size successfully processed .txt files from source dir.
    Success is determined from the processing log.
    """
    completed_dir = Path(completed_dir)
    if not completed_dir.exists():
        return []
    status_map = _load_processing_status_map()
    candidates = [
        p for p in completed_dir.glob("*.txt")
        if status_map.get(p.name) == "success"
    ]
    if not candidates:
        return []
    if len(candidates) <= sample_size:
        return candidates
    return random.sample(candidates, sample_size)


def _normalize_field_value(value):
    """Normalize a scalar value for comparison: trim, lowercase, collapse whitespace; try numeric parse."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    if not s:
        return ""
    s = " ".join(s.lower().split())
    try:
        f = float(s.replace(",", ""))
        return str(int(f)) if f == int(f) else str(f)
    except (ValueError, TypeError):
        pass
    return s


def _unit_key(unit):
    """Stable key for aligning emission units across models. Prefer Unit ID, else hash of Unit Description."""
    if not isinstance(unit, dict):
        return "unknown"
    uid = unit.get("Unit ID")
    if uid is not None and str(uid).strip():
        return str(uid).strip()
    desc = unit.get("Unit Description") or ""
    return "desc_" + hashlib.md5(desc.encode("utf-8")).hexdigest()[:12]


def _compare_general_fields(model_results):
    """
    model_results: list of dicts { "model": str, "result": dict|None, "error": str|None }.
    Returns field_summary dict: for each KEY_GENERAL_FIELDS, complete_models, agreeing_models, chosen_value, needs_review.
    """
    field_summary = {}
    successful = [m for m in model_results if m.get("result") is not None]
    for field in KEY_GENERAL_FIELDS:
        values = []
        for m in successful:
            val = (m["result"] or {}).get(field)
            values.append((m["model"], _normalize_field_value(val)))
        complete_models = [mod for mod, v in values if v]
        chosen_value = None
        if complete_models:
            norm_counts = Counter(v for mod, v in values if v)
            if norm_counts:
                chosen_value = norm_counts.most_common(1)[0][0]
        agreeing = [mod for mod, v in values if v and v == chosen_value] if chosen_value else []
        needs_review = len(set(v for _, v in values if v)) > 1 or (len(complete_models) >= 2 and len(agreeing) < 2)
        field_summary[field] = {
            "complete_models": complete_models,
            "agreeing_models": agreeing,
            "chosen_value": chosen_value,
            "needs_review": needs_review,
        }
    return field_summary


def _align_units_by_key(model_results):
    """
    Build a mapping: unit_key -> { model_name -> unit_dict } for models that have that unit.
    Only include models with successful result.
    """
    aligned = {}
    for m in model_results:
        if m.get("result") is None:
            continue
        units = m["result"].get("Emission Units") or []
        if not isinstance(units, list):
            continue
        for u in units:
            if not isinstance(u, dict):
                continue
            key = _unit_key(u)
            if key not in aligned:
                aligned[key] = {}
            aligned[key][m["model"]] = u
    return aligned


def _compare_unit_fields(aligned_units):
    """
    aligned_units: dict unit_key -> { model_name -> unit_dict }.
    Returns unit_summary: dict unit_key -> { field -> { complete_models, agreeing_models, chosen_value, needs_review } }
    and a scalar ratio of consistent key unit fields across units that appear in >=2 models.
    """
    unit_summary = {}
    total_key_fields = 0
    consistent_count = 0
    for unit_key, model_units in aligned_units.items():
        if len(model_units) < 2:
            continue
        unit_summary[unit_key] = {}
        for field in KEY_UNIT_FIELDS:
            values = [(mod, _normalize_field_value(u.get(field))) for mod, u in model_units.items()]
            complete_models = [mod for mod, v in values if v]
            norm_counts = {}
            for mod, v in values:
                if v:
                    norm_counts[v] = norm_counts.get(v, 0) + 1
            chosen_value = max(norm_counts, key=norm_counts.get) if norm_counts else None
            agreeing = [mod for mod, v in values if v and v == chosen_value] if chosen_value else []
            needs_review = len(set(v for _, v in values if v)) > 1 or (len(complete_models) >= 2 and len(agreeing) < 2)
            unit_summary[unit_key][field] = {
                "complete_models": complete_models,
                "agreeing_models": agreeing,
                "chosen_value": chosen_value,
                "needs_review": needs_review,
            }
            total_key_fields += 1
            if not needs_review and chosen_value:
                consistent_count += 1
            elif not needs_review and not any(v for _, v in values):
                consistent_count += 1
        if not KEY_UNIT_FIELDS:
            total_key_fields += 1
            consistent_count += 1
    ratio = consistent_count / total_key_fields if total_key_fields else 1.0
    return unit_summary, ratio


def _validation_decision(field_summary, unit_summary, unit_agreement_ratio, model_results):
    """
    Decide SUCCESS vs REVIEW_REQUIRED. Moderate strictness:
    - All KEY_GENERAL_FIELDS: at least two models complete and consistent (or all empty).
    - For units in >=2 models, at least VALIDATION_UNIT_AGREEMENT_THRESHOLD of KEY_UNIT_FIELDS consistent.
    - Not more than one model with parsing failure.
    """
    successful = [m for m in model_results if m.get("result") is not None]
    if len(successful) < 2:
        return "REVIEW_REQUIRED", "Fewer than two models succeeded"
    if len([m for m in model_results if m.get("error")]) >= 2:
        return "REVIEW_REQUIRED", "Two or more models failed"
    for field in KEY_GENERAL_FIELDS:
        s = field_summary.get(field, {})
        if s.get("needs_review"):
            return "REVIEW_REQUIRED", f"General field '{field}' needs review"
    if unit_agreement_ratio < VALIDATION_UNIT_AGREEMENT_THRESHOLD:
        return "REVIEW_REQUIRED", f"Unit field agreement ratio {unit_agreement_ratio:.2f} < {VALIDATION_UNIT_AGREEMENT_THRESHOLD}"
    return "SUCCESS", None


def validate_permit(txt_path, llm_client, model_names):
    """
    Run extraction with each model, compare outputs, and return a validation result dict suitable for JSONL.
    Does not move or mutate the text file.
    """
    filename = Path(txt_path).stem
    content = read_text_from_file(Path(txt_path))
    if not content:
        return {
            "filename": filename,
            "models": [],
            "field_summary": {},
            "unit_summary": {},
            "status": "REVIEW_REQUIRED",
            "reason": "Failed to read permit text",
        }
    model_results = []
    for model_name in model_names:
        result, error_str = extract_info_with_model(llm_client, content, filename, model_name)
        model_results.append({
            "model": model_name,
            "result": result,
            "error": error_str,
        })
    field_summary = _compare_general_fields(model_results)
    aligned_units = _align_units_by_key(model_results)
    unit_summary, unit_agreement_ratio = _compare_unit_fields(aligned_units)
    status, reason = _validation_decision(field_summary, unit_summary, unit_agreement_ratio, model_results)
    return {
        "filename": filename,
        "models": model_results,
        "field_summary": field_summary,
        "unit_summary": unit_summary,
        "unit_agreement_ratio": unit_agreement_ratio,
        "status": status,
        "reason": reason,
    }


def _ensure_validation_output_dir():
    """Create validation output directory if it does not exist."""
    VALIDATION_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def get_validation_run_path():
    """Path for this validation run's JSONL file (timestamped)."""
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return VALIDATION_OUTPUT_DIR / f"validation_run_{ts}.jsonl"


def write_validation_run_header(file_handle, run_id, timestamp, sample_size_requested, sample_size_actual, model_names, source_dir):
    """Write the first JSONL line with run-level metadata."""
    meta = {
        "_run_meta": True,
        "run_id": run_id,
        "timestamp": timestamp,
        "sample_size_requested": sample_size_requested,
        "sample_size_actual": sample_size_actual,
        "model_names": model_names,
        "source_dir": str(source_dir),
    }
    file_handle.write(json.dumps(meta) + "\n")


def append_validation_record(file_handle, record, run_id, timestamp):
    """Append one permit validation record to the JSONL file, adding run_id and timestamp."""
    out = dict(record)
    out["run_id"] = run_id
    out["timestamp"] = timestamp
    file_handle.write(json.dumps(out, default=str) + "\n")
    file_handle.flush()


def write_validation_report_md(run_path, records, run_id, timestamp, sample_size_requested, sample_size_actual, model_names, success_count, review_count):
    """
    Write a human-readable Markdown report for the validation run so reviewers can quickly
    see which permits need attention and why.
    """
    md_path = run_path.with_suffix(".md")
    lines = [
        "# Validation Run: Multi-LLM Consistency Check",
        "",
        f"**Run ID:** `{run_id}`  ",
        f"**Date:** {timestamp}  ",
        f"**Sample:** {sample_size_actual} permits (requested {sample_size_requested})  ",
        f"**Models:** {', '.join(model_names)}  ",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|--------|-------|",
        f"| SUCCESS | {success_count} |",
        f"| REVIEW_REQUIRED | {review_count} |",
        "",
        "---",
        "",
        "## Quick reference: all permits",
        "",
        "| Filename | Status | Reason / note |",
        "|----------|--------|---------------|",
    ]
    for rec in records:
        filename = rec.get("filename", "—")
        status = rec.get("status", "—")
        reason = (rec.get("reason") or "—").replace("|", "\\|").replace("\n", " ")
        if len(reason) > 80:
            reason = reason[:77] + "..."
        lines.append(f"| {filename} | {status} | {reason} |")

    lines.extend([
        "",
        "---",
        "",
        "## Permits requiring review (details)",
        "",
    ])
    review_records = [r for r in records if r.get("status") == "REVIEW_REQUIRED"]
    if not review_records:
        lines.append("*None.*")
    else:
        for rec in review_records:
            fn = rec.get("filename", "unknown")
            reason = rec.get("reason") or "No reason recorded."
            lines.extend([
                f"### {fn}",
                "",
                f"**Why flagged:** {reason}",
                "",
            ])
            field_summary = rec.get("field_summary") or {}
            needs_review_fields = [f for f, s in field_summary.items() if s.get("needs_review")]
            if needs_review_fields:
                lines.append("**General fields needing review:**")
                lines.append("")
                for fld in needs_review_fields:
                    s = field_summary[fld]
                    chosen = s.get("chosen_value") or "(none)"
                    agreeing = s.get("agreeing_models") or []
                    complete = s.get("complete_models") or []
                    lines.append(f"- **{fld}**  ")
                    lines.append(f"  - Chosen value: {chosen}  ")
                    lines.append(f"  - Models with value: {', '.join(complete)}  ")
                    lines.append(f"  - Models agreeing: {', '.join(agreeing) if agreeing else '—'}  ")
                    lines.append("")
                lines.append("")
            unit_summary = rec.get("unit_summary") or {}
            if unit_summary:
                lines.append("**Emission units (field agreement):**")
                lines.append("")
                for unit_key, fields in unit_summary.items():
                    bad = [f for f, s in fields.items() if s.get("needs_review")]
                    if bad:
                        lines.append(f"- Unit `{unit_key}`: fields needing review — {', '.join(bad)}")
                lines.append("")
            lines.append("---")
            lines.append("")

    lines.extend([
        "## Permits passed (no action needed)",
        "",
    ])
    success_records = [r for r in records if r.get("status") == "SUCCESS"]
    if not success_records:
        lines.append("*None.*")
    else:
        for rec in success_records:
            lines.append(f"- {rec.get('filename', '—')}")
        lines.append("")

    lines.extend([
        "---",
        "",
        "*Full machine-readable output (including raw model outputs) is in the companion `.jsonl` file.*",
        "",
    ])
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logging.info("Wrote human-readable validation report: %s", md_path)
    return md_path


def write_validation_report_excel(run_path, records, run_id, timestamp, sample_size_requested, sample_size_actual, model_names, success_count, review_count):
    """
    Write an Excel validation report so reviewers can compare each model's outputs side-by-side.
    Sheets: Run info, Summary, General fields (one row per permit per field), Unit fields (one row per permit per unit per field).
    """
    excel_path = run_path.with_suffix(".xlsx")

    # Run info sheet
    run_info_rows = [
        ("Run ID", run_id),
        ("Timestamp", timestamp),
        ("Sample requested", sample_size_requested),
        ("Sample actual", sample_size_actual),
        ("Models", ", ".join(model_names)),
        ("SUCCESS count", success_count),
        ("REVIEW_REQUIRED count", review_count),
    ]
    run_info_df = pd.DataFrame(run_info_rows, columns=["Key", "Value"])

    # Summary sheet
    summary_rows = [
        {
            "Filename": rec.get("filename", ""),
            "Status": rec.get("status", ""),
            "Reason": rec.get("reason") or "",
        }
        for rec in records
    ]
    summary_df = pd.DataFrame(summary_rows)

    # General fields comparison: one row per (filename, field) with one column per model value
    general_rows = []
    for rec in records:
        filename = rec.get("filename", "")
        field_summary = rec.get("field_summary") or {}
        models_list = rec.get("models") or []
        model_name_to_result = {m["model"]: m.get("result") for m in models_list}
        for field in KEY_GENERAL_FIELDS:
            s = field_summary.get(field, {})
            row = {
                "Filename": filename,
                "Field": field,
            }
            for mn in model_names:
                res = model_name_to_result.get(mn)
                val = (res.get(field) if isinstance(res, dict) else None) if res else None
                row[mn] = "" if val is None or (isinstance(val, float) and pd.isna(val)) else str(val).strip()
            row["Agree"] = "Y" if len(s.get("agreeing_models") or []) >= 2 else "N"
            row["Needs_review"] = "Y" if s.get("needs_review") else "N"
            row["Chosen_value"] = s.get("chosen_value") or ""
            general_rows.append(row)
    general_df = pd.DataFrame(general_rows)
    # Column order: Filename, Field, then each model, then Agree, Needs_review, Chosen_value
    general_cols = ["Filename", "Field"] + list(model_names) + ["Agree", "Needs_review", "Chosen_value"]
    general_df = general_df[[c for c in general_cols if c in general_df.columns]]

    # Unit fields comparison: one row per (filename, unit_key, field) with one column per model value
    unit_rows = []
    for rec in records:
        filename = rec.get("filename", "")
        models_list = rec.get("models") or []
        aligned = _align_units_by_key(models_list)
        unit_summary = rec.get("unit_summary") or {}
        for unit_key, model_units in aligned.items():
            if len(model_units) < 2:
                continue
            for field in KEY_UNIT_FIELDS:
                s = unit_summary.get(unit_key, {}).get(field, {})
                row = {
                    "Filename": filename,
                    "Unit_key": unit_key,
                    "Field": field,
                }
                for mn in model_names:
                    u = model_units.get(mn, {})
                    val = u.get(field) if isinstance(u, dict) else None
                    row[mn] = "" if val is None or (isinstance(val, float) and pd.isna(val)) else str(val).strip()
                row["Agree"] = "Y" if len(s.get("agreeing_models") or []) >= 2 else "N"
                row["Needs_review"] = "Y" if s.get("needs_review") else "N"
                row["Chosen_value"] = s.get("chosen_value") or ""
                unit_rows.append(row)
    unit_cols = ["Filename", "Unit_key", "Field"] + list(model_names) + ["Agree", "Needs_review", "Chosen_value"]
    if unit_rows:
        unit_df = pd.DataFrame(unit_rows)[unit_cols]
    else:
        unit_df = pd.DataFrame(columns=unit_cols)

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        run_info_df.to_excel(writer, sheet_name="Run info", index=False)
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        general_df.to_excel(writer, sheet_name="General fields", index=False)
        unit_df.to_excel(writer, sheet_name="Unit fields", index=False)

    logging.info("Wrote validation Excel report: %s", excel_path)
    return excel_path


def process_text_file(idx, total, txt_file_path, llm_client, allow_large_model_retry=False, max_chunk_chars=None):
    """Process a single text file, keeping source file in place and logging status."""
    original_filename = txt_file_path.stem
    file_rows = []
    model_used = None
    processing_status = "failed"
    try:
        print(f"\nProcessing text file {idx}/{total}: {txt_file_path.name}")
        logging.info(f"\nProcessing text file {idx}/{total}: {txt_file_path.name}")

        permit_text_content = read_text_from_file(txt_file_path)
        if not permit_text_content:
            logging.warning(f"  Skipping file {original_filename} due to text reading error or empty content.")
            file_rows.append({
                "Filename": original_filename,
                "Status": "Text Reading Failed",
                "Model Used": None,
                **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}
            })
            return file_rows

        MAX_CHARS = 1500000  # Example
        if len(permit_text_content) > MAX_CHARS:
            logging.warning(f"  Text from {original_filename} is very long ({len(permit_text_content)} chars). Processing may be slow/costly.")

        extracted_info, model_used = extract_info_with_llm(
            llm_client,
            permit_text_content,
            original_filename,
            allow_large_model_retry=allow_large_model_retry,
            max_chunk_chars=max_chunk_chars,
        )

        if extracted_info and isinstance(extracted_info, dict):
            general_info = {field: extracted_info.get(field) for field in GENERAL_TARGET_FIELDS}
            emission_units = extracted_info.get("Emission Units", [])

            if not isinstance(emission_units, list):
                logging.warning(f"  'Emission Units' field in response for {original_filename} was not a list. Treating as no units found. Value: {emission_units}")
                emission_units = []

            if emission_units:
                logging.info(f"  Extracted {len(emission_units)} emission units from {original_filename}.")
                for unit in emission_units:
                    if isinstance(unit, dict):
                        row_data = {"Filename": original_filename, "Status": "Success", "Model Used": model_used}
                        row_data.update(general_info)
                        for field in UNIT_DETAIL_FIELDS:
                            row_data[field] = unit.get(field)
                        file_rows.append(postprocess_extraction_row(row_data))
                    else:
                        logging.warning(f"  Skipping invalid unit entry (not a dict) in {original_filename}: {unit}")
                        row_data = {"Filename": original_filename, "Status": "Malformed Unit Data", "Model Used": model_used}
                        row_data.update(general_info)
                        for field in UNIT_DETAIL_FIELDS:
                            row_data[field] = "INVALID UNIT ENTRY"
                        file_rows.append(row_data)

                processing_status = "success"
            else:
                logging.info(f"  No valid emission units extracted or found for {original_filename}.")
                row_data = {"Filename": original_filename, "Status": "Success (No Units Found)", "Model Used": model_used}
                row_data.update(general_info)
                for field in UNIT_DETAIL_FIELDS:
                    row_data[field] = None
                file_rows.append(postprocess_extraction_row(row_data))
                processing_status = "success"
        else:
            logging.error(f"  Failed to extract information from {original_filename} (LLM call returned None or invalid data).")
            file_rows.append({
                "Filename": original_filename,
                "Status": "LLM Extraction Failed",
                "Model Used": model_used,
                **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}
            })

        return file_rows
    except Exception as e:
        logging.error(f"  Unexpected error processing {original_filename}: {e}", exc_info=True)
        file_rows.append({
            "Filename": original_filename,
            "Status": "Processing Error",
            "Model Used": model_used,
            **{field: "ERROR" for field in ALL_OUTPUT_FIELDS}
        })
        return file_rows
    finally:
        try:
            log_file_processing_result(txt_file_path, processing_status, model_used=model_used)
        except Exception as e:
            logging.error(f"Failed to write processing log for {txt_file_path.name}: {e}", exc_info=True)


@app.command()
def main(
    retry_failed: bool = typer.Option(False, "--retry-failed", "-r", help="Retry processing files that previously failed"),
    max_files: Optional[int] = typer.Option(
        None,
        "--max-files",
        "-m",
        help="Process at most this many pending .txt files (after the processing log filter). Omit for no limit.",
    ),
    max_chunk_chars: int = typer.Option(
        DEFAULT_MAX_CHUNK_CHARS,
        "--max-chunk-chars",
        "-c",
        help=(
            "When a document exceeds the model's context window, split it into "
            "chunks of at most this many characters and merge the results. "
            f"Default: {DEFAULT_MAX_CHUNK_CHARS:,} chars."
        ),
    ),
):
    print("Starting LLM Extraction Process from Text Files...")
    logging.info("Starting LLM Extraction Process from Text Files...")
    print(f"Chunking enabled: documents exceeding model context will be split into ≤{max_chunk_chars:,}-char chunks")

    llm_client = configure_llm()
    if not llm_client:
        print("ERROR: LLM configuration failed!")
        logging.critical("Exiting due to LLM configuration error.")
        return

    print(f"Checking for text files in: {TEXT_INPUT_DIR}")
    if not TEXT_INPUT_DIR.is_dir():
        print(f"ERROR: Text input directory not found at '{TEXT_INPUT_DIR}'")
        logging.critical(f"Error: Text input directory not found at '{TEXT_INPUT_DIR}'. This directory should contain .txt files from the ocr_processor.py script.")
        return

    setup_processing_tracking()

    # Get all .txt files from the source directory. Files are kept in place.
    all_txt_files = list(TEXT_INPUT_DIR.glob('*.txt'))
    
    # Build processed-file index from persistent log.
    # - normal mode: skip both success and failed (avoid duplicate attempts)
    # - retry mode: skip only successful files; failed files are retried
    status_map = _load_processing_status_map()
    if retry_failed:
        processed_files = {name for name, status in status_map.items() if status == "success"}
    else:
        processed_files = set(status_map.keys())
    
    # Filter out already processed files
    text_files = [f for f in all_txt_files if f.name not in processed_files]
    
    completed_count = sum(1 for status in status_map.values() if status == "success")
    failed_count = sum(1 for status in status_map.values() if status == "failed")
    
    print(f"Found {len(all_txt_files)} total .txt files")
    print(f"  - {completed_count} previously completed (from log)")
    print(f"  - {failed_count} previously failed (from log)")
    if retry_failed:
        print(f"  - Retry mode: Will retry files with failed status")
    print(f"Processing {len(text_files)} remaining files")
    if not text_files:
        print(f"No .txt files found in '{TEXT_INPUT_DIR}'.")
        logging.warning(f"No .txt files found in '{TEXT_INPUT_DIR}'.")
        return

    if max_files is not None:
        if max_files < 1:
            print("ERROR: --max-files must be at least 1 when set.")
            return
        original_pending = len(text_files)
        text_files = text_files[:max_files]
        print(f"  - Limiting run to {len(text_files)} file(s) (--max-files={max_files}; {original_pending} pending before cap)")
        logging.info(
            "Applying max_files=%s: processing %s of %s pending files",
            max_files,
            len(text_files),
            original_pending,
        )

    print(f"Processing {len(text_files)} .txt files...")
    logging.info(f"Found {len(text_files)} .txt files to process.")
    processed_data_rows = []
    excel_columns = build_excel_column_order()
    run_output_file = get_run_output_excel_path()
    print(f"Run output file: {run_output_file.name}")
    logging.info(f"Run output file: {run_output_file}")
    output_df = pd.DataFrame(columns=excel_columns)
    files_since_save = 0

    with ThreadPoolExecutor(max_workers=LLM_MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                process_text_file,
                i,
                len(text_files),
                txt_file_path,
                llm_client,
                ALLOW_LARGE_MODEL_RETRY,
                max_chunk_chars,
            )
            for i, txt_file_path in enumerate(text_files, 1)
        ]

        for future in tqdm(as_completed(futures), total=len(futures), desc="LLM extraction"):
            try:
                file_rows = future.result()
            except Exception as e:
                logging.error(f"Unexpected worker failure: {e}", exc_info=True)
                file_rows = []

            if file_rows:
                output_df = merge_rows(output_df, file_rows, excel_columns)
                processed_data_rows.extend(file_rows)
                print(f"  ✓ Saved {len(file_rows)} row(s) to Excel")
                files_since_save += 1

                if files_since_save >= SAVE_EVERY_N_FILES:
                    # Fail loudly: a swallowed write error means subsequent batches also
                    # silently fail and the in-memory rows are lost when the run ends.
                    write_permit_excel_multisheet(output_df, run_output_file, excel_columns)
                    logging.info(f"  Wrote batch to run Excel after {files_since_save} files")
                    files_since_save = 0

    # Add spec sheet links if enabled (rows are already saved incrementally)
    if processed_data_rows:
        write_permit_excel_multisheet(output_df, run_output_file, excel_columns)
        logging.info(f"Final Excel write complete (run file): {run_output_file}")
        combine_run_into_combined(run_output_file, excel_columns)

    if processed_data_rows and ENABLE_SPEC_SHEET_LOOKUP:
        logging.info(f"\nAdding spec sheet links to existing Excel file...")
        try:
            # Read the existing Excel file
            if os.path.exists(OUTPUT_EXCEL_FILE):
                df = pd.read_excel(OUTPUT_EXCEL_FILE, engine='openpyxl')
                
                # Add spec sheet links for rows with make and model data
                logging.info("Adding spec sheet links for equipment with make and model information...")
                df = add_spec_sheet_links(df, llm_client)
                
                # Ensure all columns are in the right order
                excel_columns = build_excel_column_order()
                for col in excel_columns:
                    if col not in df.columns:
                        df[col] = None
                df = df[excel_columns]
                
                write_permit_excel_multisheet(df, OUTPUT_EXCEL_FILE, excel_columns)
                logging.info(f"Successfully added spec sheet links to '{OUTPUT_EXCEL_FILE}'.")
            else:
                logging.warning("Excel file not found for spec sheet link addition.")
        except Exception as e:
            logging.error(f"Error adding spec sheet links to Excel: {e}", exc_info=True)
    elif processed_data_rows:
        print(f"\nProcessed {len(processed_data_rows)} row(s) (saved incrementally)")
        logging.info(f"Processed {len(processed_data_rows)} row(s) (saved incrementally)")
    else:
        print("No data was processed to save.")
        logging.warning("No data was processed to save.")

    print("\nStep 4: Cleaning latest Excel output...")
    logging.info("Starting cleaning step for latest Excel output.")
    clean_latest_excel_output()

    print(f"\nLLM Extraction process finished!")
    print(f"Processed {len(text_files)} files")
    print(f"Source files kept in place under: {TEXT_INPUT_DIR}")
    print(f"Processing log updated at: {PROCESSING_LOG_PATH}")
    logging.info("\nLLM Extraction process finished.")


@app.command()
def validate(
    sample_size: int = typer.Option(VALIDATION_SAMPLE_SIZE, "--sample-size", "-n", help="Number of permits to randomly sample for validation"),
):
    """Run multi-LLM validation: sample permits, extract with three models, compare for consistency, write JSONL report."""
    print("Starting validation run (multi-LLM consistency check)...")
    logging.info("Starting validation run (multi-LLM consistency check).")

    llm_client = configure_llm()
    if not llm_client:
        print("ERROR: LLM configuration failed!")
        logging.critical("Exiting due to LLM configuration error.")
        return

    _ensure_validation_output_dir()
    run_path = get_validation_run_path()
    run_id = run_path.stem
    timestamp = datetime.now().isoformat()

    sampled = sample_completed_permits(TEXT_INPUT_DIR, sample_size)
    sample_size_actual = len(sampled)
    if sample_size_actual == 0:
        print(f"No successfully processed permit text files found under {TEXT_INPUT_DIR}. Run the main extraction pipeline first.")
        logging.warning("No successfully processed permit files for validation.")
        return

    print(f"Sampled {sample_size_actual} permit(s) from {TEXT_INPUT_DIR} (successes from log). Models: {VALIDATION_MODELS}")
    logging.info(f"Validation run {run_id}: sample_size={sample_size_actual}, models={VALIDATION_MODELS}")

    success_count = 0
    review_count = 0
    all_records = []
    with open(run_path, "w", encoding="utf-8") as f:
        write_validation_run_header(
            f, run_id, timestamp,
            sample_size_requested=sample_size,
            sample_size_actual=sample_size_actual,
            model_names=VALIDATION_MODELS,
            source_dir=TEXT_INPUT_DIR,
        )
        for i, txt_path in enumerate(tqdm(sampled, desc="Validating"), 1):
            print(f"\nValidation {i}/{sample_size_actual}: {txt_path.name}")
            record = validate_permit(txt_path, llm_client, VALIDATION_MODELS)
            all_records.append(record)
            append_validation_record(f, record, run_id, timestamp)
            if record.get("status") == "SUCCESS":
                success_count += 1
            else:
                review_count += 1

    md_path = write_validation_report_md(
        run_path, all_records, run_id, timestamp,
        sample_size, sample_size_actual, VALIDATION_MODELS,
        success_count, review_count,
    )
    excel_path = write_validation_report_excel(
        run_path, all_records, run_id, timestamp,
        sample_size, sample_size_actual, VALIDATION_MODELS,
        success_count, review_count,
    )

    print(f"\nValidation run finished!")
    print(f"  Total permits: {sample_size_actual}")
    print(f"  SUCCESS: {success_count}")
    print(f"  REVIEW_REQUIRED: {review_count}")
    print(f"  Excel (compare models): {excel_path}")
    print(f"  Human-readable report: {md_path}")
    print(f"  Machine-readable (JSONL): {run_path}")
    logging.info("Validation run finished: run_id=%s, success=%s, review_required=%s, path=%s", run_id, success_count, review_count, run_path)


@app.command()
def add_spec_sheets():
    """Add spec sheet links to an existing Excel file."""
    print("Adding spec sheet links to existing Excel file...")
    logging.info("Starting spec sheet link addition process...")

    if not ENABLE_SPEC_SHEET_LOOKUP:
        print("Spec sheet lookup is currently disabled. Enable ENABLE_SPEC_SHEET_LOOKUP to use this command.")
        logging.info("Spec sheet lookup disabled; exiting without changes.")
        return
    
    # Configure LLM client for analysis
    llm_client = configure_llm()
    if not llm_client:
        print("ERROR: LLM configuration failed!")
        logging.critical("Exiting due to LLM configuration error.")
        return
    
    # Check if the Excel file exists
    if not os.path.exists(OUTPUT_EXCEL_FILE):
        print(f"ERROR: Excel file not found at '{OUTPUT_EXCEL_FILE}'")
        print("Please run the main pipeline first to generate the Excel file.")
        logging.error(f"Excel file not found at '{OUTPUT_EXCEL_FILE}'")
        return
    
    try:
        # Read the existing Excel file
        logging.info(f"Reading existing Excel file: {OUTPUT_EXCEL_FILE}")
        df = read_permit_excel_full_table(OUTPUT_EXCEL_FILE)
        
        print(f"Found {len(df)} rows in the Excel file")
        logging.info(f"Found {len(df)} rows in the Excel file")
        
        # Add spec sheet links
        logging.info("Adding spec sheet links...")
        df = add_spec_sheet_links(df, llm_client)
        
        # Create backup of original file
        backup_file = OUTPUT_EXCEL_FILE.replace('.xlsx', '_backup.xlsx')
        shutil.copy2(OUTPUT_EXCEL_FILE, backup_file)
        logging.info(f"Created backup of original file: {backup_file}")
        
        excel_columns = build_excel_column_order()
        for col in excel_columns:
            if col not in df.columns:
                df[col] = None
        df = df[excel_columns]
        write_permit_excel_multisheet(df, OUTPUT_EXCEL_FILE, excel_columns)
        print(f"Successfully updated Excel file with spec sheet links!")
        print(f"Original file backed up as: {backup_file}")
        logging.info(f"Successfully updated Excel file with spec sheet links")
        
        # Report statistics
        spec_links_added = (df['Spec Sheet Link'] != "").sum()
        print(f"Added spec sheet links for {spec_links_added} equipment items")
        logging.info(f"Added spec sheet links for {spec_links_added} equipment items")
        
    except Exception as e:
        print(f"ERROR: Failed to add spec sheet links: {e}")
        logging.error(f"Failed to add spec sheet links: {e}", exc_info=True)


# --- Async batch processing ---
# Uses asyncio + AsyncOpenAI to run many concurrent LLM requests locally,
# bypassing the need for server-side Batch API file storage.

ASYNC_BATCH_CONCURRENCY = 5  # CBORG max parallel requests


def _parse_llm_json_response(response):
    """Parse a JSON extraction response from the LLM. Returns (data, error_str)."""
    if not response or not response.choices:
        return None, "empty response"
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        # Output truncated at provider's max-output cap; JSON is incomplete.
        # Surface as token-limit error so the chunking path picks it up.
        return None, "Output truncated (finish_reason=length)"
    try:
        json_text = choice.message.content.strip()
        if "```json" in json_text:
            json_text = json_text.split("```json")[1].split("```")[0].strip()
        elif "```" in json_text:
            json_text = json_text.split("```")[1].split("```")[0].strip()
        extracted_data = json.loads(json_text)
        if "Emission Units" not in extracted_data or not isinstance(extracted_data.get("Emission Units"), list):
            extracted_data["Emission Units"] = []
        return extracted_data, None
    except (json.JSONDecodeError, Exception) as e:
        return None, str(e)


async def _async_extract_one(
    async_client,
    txt_path: Path,
    semaphore: asyncio.Semaphore,
    pbar,
    allow_large_model_retry: bool = False,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
):
    """Extract permit data from a single text file using the async OpenAI client.

    Extraction cascade:
    1. Try the primary model (LLM_MODEL) with the full document.
    2. On token-limit error, split the document into chunks, extract each
       chunk separately, and merge the results.
    3. If chunking also fails, fall back to LLM_LARGE_MODEL with the full
       document (when *allow_large_model_retry* is True).
    """
    original_filename = txt_path.stem

    # Read file in a thread to avoid blocking the event loop and to respect
    # the semaphore (prevents thousands of simultaneous file opens).
    async with semaphore:
        text_content = await asyncio.to_thread(read_text_from_file, txt_path)
    if not text_content:
        logging.warning(f"  Skipping {original_filename}: unreadable or empty")
        pbar.update(1)
        return original_filename, None, "failed", None

    def _make_messages(text):
        prompt = PROMPT_TEMPLATE.replace("{permit_text}", text)
        return [
            {"role": "system", "content": "You are an expert at extracting structured information from industrial air permit documents. Always respond with valid JSON."},
            {"role": "user", "content": prompt},
        ]

    async def _call(model_name, text):
        messages = _make_messages(text)
        last_err = None
        for attempt in range(1, LLM_MAX_RETRIES + 1):
            try:
                async with semaphore:
                    await asyncio.sleep(0.05)  # tiny stagger
                    response = await async_client.chat.completions.create(
                        model=model_name,
                        messages=messages,
                        temperature=0.1,
                        max_tokens=32768,
                        timeout=120,
                        response_format={"type": "json_object"},
                    )
                return response, None
            except Exception as e:
                last_err = e
                if _is_retryable_error(e) and attempt < LLM_MAX_RETRIES:
                    delay = min(LLM_BACKOFF_BASE ** attempt + random.uniform(0, 1), LLM_BACKOFF_MAX)
                    logging.warning(f"  Retryable error for {original_filename} (attempt {attempt}): {e}. Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                    continue
                return None, str(e)
        return None, str(last_err)

    # --- 1. Try full-document extraction with the primary model ---
    response, error_str = await _call(LLM_MODEL, text_content)
    model_used = LLM_MODEL

    if response is not None:
        extracted_data, parse_err = _parse_llm_json_response(response)
        if extracted_data is not None:
            pbar.update(1)
            return original_filename, extracted_data, "success", model_used
        logging.error(f"  JSON parse error for {original_filename}: {parse_err}")
        # Forward parse-time token-limit signals (e.g. truncated output) to the chunking dispatch.
        if not error_str:
            error_str = parse_err

    # --- 2. Chunked extraction (same small model) ---
    if error_str and _is_token_limit_error(error_str):
        chunk_size = max_chunk_chars or DEFAULT_MAX_CHUNK_CHARS
        chunks = _split_text_into_chunks(text_content, chunk_size)
        logging.warning(
            f"  Token limit hit for {original_filename} using {LLM_MODEL}. "
            f"Splitting into {len(chunks)} chunk(s) (max {chunk_size} chars each)."
        )
        chunk_results = []
        for i, chunk in enumerate(chunks, 1):
            logging.info(f"  Chunk {i}/{len(chunks)} for {original_filename} ({len(chunk)} chars)")
            resp, chunk_err = await _call(LLM_MODEL, chunk)
            if resp is not None:
                data, parse_err = _parse_llm_json_response(resp)
                if data is not None:
                    chunk_results.append(data)
                else:
                    logging.warning(f"  Chunk {i}/{len(chunks)} parse failed for {original_filename}: {parse_err}")
            else:
                logging.warning(f"  Chunk {i}/{len(chunks)} failed for {original_filename}: {chunk_err}")

        if chunk_results:
            merged = _merge_chunk_extractions(chunk_results)
            logging.info(
                f"  Merged {len(chunk_results)}/{len(chunks)} chunk(s) for {original_filename}"
            )
            pbar.update(1)
            return original_filename, merged, "success", f"{LLM_MODEL} (chunked)"
        logging.warning(f"  All chunks failed for {original_filename}")

    # --- 3. Large-model fallback ---
    if error_str and _is_token_limit_error(error_str) and allow_large_model_retry and LLM_LARGE_MODEL != LLM_MODEL:
        logging.warning(f"  Token limit for {original_filename}, retrying with {LLM_LARGE_MODEL}")
        response, error_str = await _call(LLM_LARGE_MODEL, text_content)
        model_used = LLM_LARGE_MODEL
        if response is not None:
            extracted_data, parse_err = _parse_llm_json_response(response)
            if extracted_data is not None:
                pbar.update(1)
                return original_filename, extracted_data, "success", model_used
            logging.error(f"  JSON parse error for {original_filename} with {model_used}: {parse_err}")

    if response is None:
        logging.error(f"  Failed LLM call for {original_filename}: {error_str}")

    pbar.update(1)
    return original_filename, None, "failed", model_used


def _build_rows_from_extracted(original_filename: str, extracted_info: dict, model_used: str) -> list:
    """Convert extracted JSON into output rows (shared by batch and main paths)."""
    file_rows = []
    general_info = {field: extracted_info.get(field) for field in GENERAL_TARGET_FIELDS}
    emission_units = extracted_info.get("Emission Units", [])
    if not isinstance(emission_units, list):
        emission_units = []

    if emission_units:
        for unit in emission_units:
            if isinstance(unit, dict):
                row_data = {"Filename": original_filename, "Status": "Success", "Model Used": model_used}
                row_data.update(general_info)
                for field in UNIT_DETAIL_FIELDS:
                    row_data[field] = unit.get(field)
                file_rows.append(postprocess_extraction_row(row_data))
            else:
                row_data = {"Filename": original_filename, "Status": "Malformed Unit Data", "Model Used": model_used}
                row_data.update(general_info)
                for field in UNIT_DETAIL_FIELDS:
                    row_data[field] = "INVALID UNIT ENTRY"
                file_rows.append(row_data)
    else:
        row_data = {"Filename": original_filename, "Status": "Success (No Units Found)", "Model Used": model_used}
        row_data.update(general_info)
        for field in UNIT_DETAIL_FIELDS:
            row_data[field] = None
        file_rows.append(postprocess_extraction_row(row_data))

    return file_rows


async def _run_async_batch(text_files, allow_large_model_retry, concurrency,
                           excel_columns, run_output_file):
    """Run all extractions concurrently, saving results incrementally as they complete."""
    async_client = openai.AsyncOpenAI(
        api_key=OPENAI_API_KEY,
        base_url="https://api.cborg.lbl.gov",
    )
    semaphore = asyncio.Semaphore(concurrency)
    pbar = tqdm(total=len(text_files), desc="Async LLM extraction")

    # Shared mutable state for incremental saves (single-threaded event loop, so no lock needed)
    state = {
        "output_df": pd.DataFrame(columns=excel_columns),
        "all_rows": [],
        "success_count": 0,
        "fail_count": 0,
        "fallback_count": 0,
        "files_since_save": 0,
    }

    def _update_pbar_stats():
        total_done = state["success_count"] + state["fail_count"]
        rate = (state["fail_count"] / total_done * 100) if total_done else 0
        pbar.set_postfix(
            ok=state["success_count"],
            fail=state["fail_count"],
            fallback=state["fallback_count"],
            fail_pct=f"{rate:.1f}%",
            refresh=False,
        )

    async def _extract_and_save(txt_path):
        """Extract one file and immediately persist results."""
        result = await _async_extract_one(
            async_client, txt_path, semaphore, pbar, allow_large_model_retry
        )

        if isinstance(result, Exception):
            logging.error(f"Unexpected async error: {result}")
            state["fail_count"] += 1
            _update_pbar_stats()
            return

        original_filename, extracted_info, status, model_used = result
        txt_file_path = TEXT_INPUT_DIR / f"{original_filename}.txt"

        # Log to JSONL immediately so restarts skip this file
        log_file_processing_result(txt_file_path, status, model_used=model_used)

        if status == "success" and extracted_info:
            file_rows = _build_rows_from_extracted(original_filename, extracted_info, model_used)
            state["all_rows"].extend(file_rows)
            state["output_df"] = merge_rows(state["output_df"], file_rows, excel_columns)
            state["success_count"] += 1
            if model_used != LLM_MODEL:
                state["fallback_count"] += 1
        else:
            state["all_rows"].append({
                "Filename": original_filename,
                "Status": "LLM Extraction Failed",
                "Model Used": model_used,
                **{field: "ERROR" for field in ALL_OUTPUT_FIELDS},
            })
            state["fail_count"] += 1

        _update_pbar_stats()

        # Incremental Excel save — fail loudly so a write error doesn't silently
        # discard rows accumulated in state["output_df"].
        state["files_since_save"] += 1
        if state["files_since_save"] >= SAVE_EVERY_N_FILES:
            await asyncio.to_thread(
                write_permit_excel_multisheet,
                state["output_df"], run_output_file, excel_columns,
            )
            logging.info(f"Incremental save: {state['success_count']} successes so far")
            state["files_since_save"] = 0

    tasks = [_extract_and_save(txt_path) for txt_path in text_files]
    await asyncio.gather(*tasks, return_exceptions=True)
    pbar.close()
    return state


@app.command()
def batch(
    retry_failed: bool = typer.Option(False, "--retry-failed", "-r", help="Include files that previously failed"),
    max_files: Optional[int] = typer.Option(None, "--max-files", "-m", help="Limit number of files to submit"),
    concurrency: int = typer.Option(ASYNC_BATCH_CONCURRENCY, "--concurrency", "-c", help="Max concurrent async LLM requests"),
):
    """High-throughput async extraction: process all pending documents with many concurrent LLM requests."""
    print("Starting async batch extraction...")

    # Validate LLM connectivity (sync client for the quick test)
    llm_client = configure_llm()
    if not llm_client:
        print("ERROR: LLM configuration failed!")
        return

    if not TEXT_INPUT_DIR.is_dir():
        print(f"ERROR: Text input directory not found at '{TEXT_INPUT_DIR}'")
        return

    setup_processing_tracking()

    # Determine which files to process (same logic as `main`)
    all_txt_files = list(TEXT_INPUT_DIR.glob("*.txt"))
    status_map = _load_processing_status_map()
    if retry_failed:
        processed_files = {name for name, status in status_map.items() if status == "success"}
    else:
        processed_files = set(status_map.keys())
    text_files = [f for f in all_txt_files if f.name not in processed_files]

    if max_files is not None and max_files >= 1:
        text_files = text_files[:max_files]

    completed_count = sum(1 for s in status_map.values() if s == "success")
    failed_count = sum(1 for s in status_map.values() if s == "failed")
    print(f"Found {len(all_txt_files)} total .txt files")
    print(f"  - {completed_count} previously completed")
    print(f"  - {failed_count} previously failed")
    if retry_failed:
        print(f"  - Retry mode: will retry failed files")
    print(f"Processing {len(text_files)} files with concurrency={concurrency}")

    if not text_files:
        print("No pending files to process.")
        return

    excel_columns = build_excel_column_order()
    run_output_file = get_run_output_excel_path()
    print(f"Run output file: {run_output_file.name}")

    # Run the async event loop with incremental saving
    state = asyncio.run(_run_async_batch(
        text_files, ALLOW_LARGE_MODEL_RETRY, concurrency,
        excel_columns, run_output_file,
    ))

    success_count = state["success_count"]
    fail_count = state["fail_count"]
    output_df = state["output_df"]

    # Final Excel write
    if state["all_rows"]:
        write_permit_excel_multisheet(output_df, run_output_file, excel_columns)
        combine_run_into_combined(run_output_file, excel_columns)

    print(f"\nAsync batch extraction complete!")
    print(f"  Success: {success_count}")
    print(f"  Failed:  {fail_count}")
    print(f"  Total:   {len(text_files)}")
    if state["all_rows"]:
        print(f"  Output:  {run_output_file.name}")

    print("\nCleaning latest Excel output...")
    clean_latest_excel_output()
    print("Done!")


if __name__ == "__main__":
    app()
