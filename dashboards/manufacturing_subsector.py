"""Interactive dashboard for exploring manufacturing subsectors in the permit dataset.

Run with:
    streamlit run dashboards/manufacturing_subsector.py
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_PATH = PROJECT_ROOT / "data" / "processed" / "permit_data_extracted.xlsx"
FRS_FACILITIES_PATH = PROJECT_ROOT / "data" / "external" / "FRS_FACILITIES.csv"
DASHBOARD_PARQUET_DIR = PROJECT_ROOT / "data" / "processed" / "dashboard"
PERMITS_PARQUET = DASHBOARD_PARQUET_DIR / "permits.parquet"
CENTROIDS_PARQUET = DASHBOARD_PARQUET_DIR / "city_centroids.parquet"
PERMITS_FILENAME = "permits.parquet"
CENTROIDS_FILENAME = "city_centroids.parquet"

# Optional remote artifact location. When set, the dashboard reads parquet
# from there instead of the local copy. Two URI schemes are supported:
#   * https://<bucket>.s3.<region>.amazonaws.com/<prefix>/ — public bucket
#     accessed over HTTPS, no AWS plumbing required.
#   * s3://<bucket>/<prefix>/ — read via s3fs. Defaults to anonymous mode
#     (works on public buckets); set AWS credentials via the standard boto3
#     chain to read from a private bucket.
S3_URI_ENV = "PERMIT_DASHBOARD_S3_URI"

SHEET_NAMES = ("Manufacturing NAICS 31-33", "Other NAICS")
BUILD_HINT = (
    "Run `python scripts/build_dashboard_parquet.py` to generate the cached "
    "parquet artifacts for faster loading."
)


def _s3_uri_for(filename: str) -> str | None:
    """Return the remote URI for a dashboard artifact, or None if not configured.

    Resolves the base URI from (in order) ``st.secrets[S3_URI_ENV]`` for
    Streamlit Cloud, then ``os.environ[S3_URI_ENV]``.
    """
    base: str | None = None
    try:
        if S3_URI_ENV in st.secrets:
            base = str(st.secrets[S3_URI_ENV])
    except Exception:
        # No secrets.toml, or a Streamlit version whose secrets error class
        # differs; either way fall through to the environment variable.
        pass
    if not base:
        base = os.environ.get(S3_URI_ENV)
    if not base:
        return None
    return base.rstrip("/") + "/" + filename


def _parquet_to_df(source) -> pd.DataFrame:
    """Read parquet, using categoricals only for dashboard-ready files.

    String columns are read straight into pandas categoricals from the parquet
    dictionary. Decoding them into Python strings instead costs ~4-8x the
    memory (3.6 GB vs 0.6 GB at 1.1M rows x 64 columns) and is what exhausts
    Streamlit's budget. Categoricals are what keeps the frame small, but the
    legacy derivation path below calls .fillna("") on text columns, which
    raises on a Categorical. Legacy files are small enough not to need the
    optimization, so only files that already carry the derived columns are
    read this way.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pq.ParquetFile(source).schema_arrow
    if hasattr(source, "seek"):
        source.seek(0)
    ready = _DERIVED_COLUMNS.issubset(schema.names)
    cols = (
        [f.name for f in schema
         if pa.types.is_string(f.type) or pa.types.is_large_string(f.type)]
        if ready else []
    )
    return pq.read_table(source, read_dictionary=cols).to_pandas()


def _read_remote_parquet(uri: str) -> pd.DataFrame:
    """Read parquet from an S3 or HTTP(S) URL, treating S3 as a public bucket."""
    if uri.startswith(("http://", "https://")):
        import requests

        resp = requests.get(uri, timeout=120)
        resp.raise_for_status()
        return _parquet_to_df(io.BytesIO(resp.content))
    if uri.startswith("s3://"):
        # anon=True works on public buckets and is ignored on private ones if
        # AWS credentials happen to be present in the environment.
        import pyarrow.fs as pafs

        path = uri[len("s3://"):]
        fs = pafs.S3FileSystem(anonymous=True)
        with fs.open_input_file(path) as f:
            return _parquet_to_df(f)
    raise ValueError(f"Unsupported remote URI scheme: {uri!r}")

# Special non-NAICS option codes used by the subsector selector.
ALL_CODE = "ALL"
MFG_CODE = "MFG"
DATA_CENTER_CODE = "DATA_CENTER"
WATER_CODE = "WATER"
WASTEWATER_CODE = "WASTEWATER"

# Keyword regexes (case-insensitive) applied to Industry Description and
# Facility Name to identify rows for non-NAICS virtual subsectors. These
# sectors are typically classified under a power-generation NAICS (e.g. 221112
# for backup generators), so NAICS alone is not enough to find them.
KEYWORD_FILTERS = {
    DATA_CENTER_CODE: re.compile(
        r"\bdata\s*cent(?:er|re)s?\b|\bcolocation\b|\bhyperscale\b", re.IGNORECASE
    ),
    WATER_CODE: re.compile(
        r"\bwater\s+(?:treatment|plant|supply|distribution|district|utility|authority)\b"
        r"|\bdrinking\s+water\b|\bpotable\s+water\b",
        re.IGNORECASE,
    ),
    WASTEWATER_CODE: re.compile(
        r"\bwastewater\b|\bsewage\b|\bsanitary\s+sewer\b"
        r"|\bpublicly\s+owned\s+treatment\b|\bWWTP\b|\bPOTW\b",
        re.IGNORECASE,
    ),
}

# 3-digit NAICS subsector labels (manufacturing 31-33).
NAICS_SUBSECTOR_LABELS = {
    "311": "Food Manufacturing",
    "312": "Beverage and Tobacco Product",
    "313": "Textile Mills",
    "314": "Textile Product Mills",
    "315": "Apparel",
    "316": "Leather and Allied Product",
    "321": "Wood Product",
    "322": "Paper",
    "323": "Printing and Related Support",
    "324": "Petroleum and Coal Products",
    "325": "Chemical",
    "326": "Plastics and Rubber Products",
    "327": "Nonmetallic Mineral Product",
    "331": "Primary Metal",
    "332": "Fabricated Metal Product",
    "333": "Machinery",
    "334": "Computer and Electronic Product",
    "335": "Electrical Equipment, Appliance, and Component",
    "336": "Transportation Equipment",
    "337": "Furniture and Related Product",
    "339": "Miscellaneous",
}

# Labels for the non-manufacturing 3-digit subsectors that appear now that the
# dashboard covers the full corpus. Codes without an entry fall back to
# "NAICS <code>".
OTHER_SUBSECTOR_LABELS = {
    "111": "Crop Production",
    "112": "Animal Production",
    "113": "Forestry and Logging",
    "115": "Support Activities for Agriculture",
    "211": "Oil and Gas Extraction",
    "212": "Mining (except Oil and Gas)",
    "213": "Support Activities for Mining",
    "221": "Utilities",
    "236": "Construction of Buildings",
    "237": "Heavy and Civil Engineering Construction",
    "238": "Specialty Trade Contractors",
    "423": "Merchant Wholesalers, Durable Goods",
    "424": "Merchant Wholesalers, Nondurable Goods",
    "441": "Motor Vehicle and Parts Dealers",
    "447": "Gasoline Stations",
    "481": "Air Transportation",
    "482": "Rail Transportation",
    "483": "Water Transportation",
    "484": "Truck Transportation",
    "486": "Pipeline Transportation",
    "488": "Support Activities for Transportation",
    "493": "Warehousing and Storage",
    "511": "Publishing Industries",
    "517": "Telecommunications",
    "518": "Data Processing, Hosting, and Related Services",
    "541": "Professional, Scientific, and Technical Services",
    "562": "Waste Management and Remediation Services",
    "611": "Educational Services",
    "622": "Hospitals",
    "721": "Accommodation",
    "811": "Repair and Maintenance",
    "921": "Executive, Legislative, and General Government",
    "922": "Justice, Public Order, and Safety Activities",
    "928": "National Security and International Affairs",
}

# Some permits state only a 2-digit sector; NAICS_clean[:3] then yields a
# 2-character "subsector". Label those honestly rather than hiding them.
SECTOR2_LABELS = {
    "11": "Agriculture (2-digit code only)",
    "21": "Mining, Oil and Gas (2-digit code only)",
    "22": "Utilities (2-digit code only)",
    "23": "Construction (2-digit code only)",
    "31": "Manufacturing (2-digit code only)",
    "32": "Manufacturing (2-digit code only)",
    "33": "Manufacturing (2-digit code only)",
    "42": "Wholesale Trade (2-digit code only)",
    "48": "Transportation (2-digit code only)",
    "49": "Transportation and Warehousing (2-digit code only)",
    "56": "Admin and Waste Services (2-digit code only)",
    "92": "Public Administration (2-digit code only)",
}


def subsector_label(code: str) -> str:
    if code in NAICS_SUBSECTOR_LABELS:
        return NAICS_SUBSECTOR_LABELS[code]
    if code in OTHER_SUBSECTOR_LABELS:
        return OTHER_SUBSECTOR_LABELS[code]
    if code in SECTOR2_LABELS:
        return SECTOR2_LABELS[code]
    return f"NAICS {code}"

# Canonical capacity-unit normalization. Keys are lowercased/stripped strings.
CAPACITY_UNIT_NORMALIZATION = {
    "mmbtu/hr": "MMBtu/hr",
    "mmbtu per hour": "MMBtu/hr",
    "mmbtuhr": "MMBtu/hr",
    "mmbtu/h": "MMBtu/hr",
    "mmbtu hr": "MMBtu/hr",
    "lb/hr": "lb/hr",
    "lbs/hr": "lb/hr",
    "pounds per hour": "lb/hr",
    "pounds/hour": "lb/hr",
    "lbs/hour": "lb/hr",
    "tons/hr": "tons/hr",
    "tons/hour": "tons/hr",
    "tons per hour": "tons/hr",
    "ton/hr": "tons/hr",
    "tph": "tons/hr",
    "hp": "HP",
    "horsepower": "HP",
    "bhp": "BHP",
    "kw": "kW",
    "mw": "MW",
    "gallons": "gallons",
    "gal": "gallons",
    "mcf/hr": "MCF/hr",
    "scfm": "SCFM",
    "acfm": "ACFM",
}


def _norm_token(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _fold_label(value: object) -> str | None:
    """Fold a free-text label for grouping: trim, collapse whitespace, strip
    trailing punctuation, lowercase. 'Winery ' / 'WINERY' / 'winery.' -> 'winery'."""
    if pd.isna(value):
        return None
    s = re.sub(r"\s+", " ", str(value).strip()).rstrip(".;,")
    return s.lower() or None


# Folded-label synonym maps: exact folded matches only, deliberately narrow so
# semantically distinct values (e.g. 'tall oil refinery') never get merged.
# Shared by the app's legacy derivation path and the parquet build script.
INDUSTRY_SYNONYMS = {
    "winery": "Winery",
    "winery facility": "Winery",
    "winery operations": "Winery",
    "winery production": "Winery",
    "wine production": "Winery",
    "petroleum refinery": "Petroleum Refinery",
    "petroleum refineries": "Petroleum Refinery",
    "petroleum refining": "Petroleum Refinery",
    "petroleum refinery facility": "Petroleum Refinery",
    "fully integrated petroleum refinery facility": "Petroleum Refinery",
    "integrated petroleum refinery": "Petroleum Refinery",
    "refinery": "Petroleum Refinery",
    "oil refinery": "Petroleum Refinery",
    "crude oil refinery": "Petroleum Refinery",
}
UNIT_TYPE_SYNONYMS: dict[str, str] = {}

FUEL_SYNONYMS = {
    "nat. gas": "Natural Gas",
    "nat gas": "Natural Gas",
    "natural gas only": "Natural Gas",
    "pipeline natural gas": "Natural Gas",
    "pipeline-quality natural gas": "Natural Gas",
    "pipeline quality natural gas": "Natural Gas",
    "no. 2 fuel oil": "No. 2 Fuel Oil",
    "no 2 fuel oil": "No. 2 Fuel Oil",
    "#2 fuel oil": "No. 2 Fuel Oil",
    "number 2 fuel oil": "No. 2 Fuel Oil",
    "fuel oil no. 2": "No. 2 Fuel Oil",
    "fuel oil #2": "No. 2 Fuel Oil",
    "diesel fuel": "Diesel",
    "diesel oil": "Diesel",
    "diesel fuel oil": "Diesel",
    "ultra low sulfur diesel": "Diesel (ULSD)",
    "ultra-low sulfur diesel": "Diesel (ULSD)",
    "ulsd": "Diesel (ULSD)",
    "liquefied petroleum gas": "LPG",
    "lpg": "LPG",
}

MAKE_SYNONYMS = {
    "caterpillar": "Caterpillar",
    "cat": "Caterpillar",
    "cummins inc": "Cummins",
    "cummins engine company": "Cummins",
    "cummins engine co": "Cummins",
    "detroit diesel corporation": "Detroit Diesel",
    "detroit diesel corp": "Detroit Diesel",
    "general electric": "GE",
    "ge": "GE",
    "babcock & wilcox": "Babcock & Wilcox",
    "babcock and wilcox": "Babcock & Wilcox",
    "b&w": "Babcock & Wilcox",
    "cleaver brooks": "Cleaver-Brooks",
    "cleaver-brooks": "Cleaver-Brooks",
    "solar turbines incorporated": "Solar Turbines",
    "solar turbines inc": "Solar Turbines",
    "ingersoll rand": "Ingersoll-Rand",
    "ingersoll-rand": "Ingersoll-Rand",
    "john deere": "John Deere",
}

# Columns the parquet build canonicalizes into "<col> (norm)", with their
# curated synonym maps. Majority-casing folding applies to all of them.
CANON_COLUMNS = [
    ("Industry Description", INDUSTRY_SYNONYMS),
    ("Unit Type", UNIT_TYPE_SYNONYMS),
    ("Fuel Type", FUEL_SYNONYMS),
    ("Control Device(s)", {}),
    ("Unit Make", MAKE_SYNONYMS),
    ("Regulatory Authority", {}),
    ("Facility City", {}),
    ("Facility County", {}),
]

VALID_STATE_CODES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL",
    "IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE",
    "NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD",
    "TN","TX","UT","VT","VA","WA","WV","WI","WY","PR","VI","GU","AS","MP",
}


def _normalize_freetext(value: object, synonyms: dict[str, str]) -> str | None:
    """Legacy-path normalizer: fold + synonyms, keeping the trimmed original
    casing otherwise. The parquet build does better (majority casing across
    the whole dataset); this keeps the xlsx fallback consistent in spirit."""
    if pd.isna(value):
        return None
    folded = _fold_label(value)
    if folded is None:
        return None
    if folded in synonyms:
        return synonyms[folded]
    return re.sub(r"\s+", " ", str(value).strip()).rstrip(".;,") or None


def _normalize_capacity_unit(value: object) -> str | None:
    if pd.isna(value):
        return None
    token = _norm_token(str(value))
    return CAPACITY_UNIT_NORMALIZATION.get(token, str(value).strip())


def _normalize_fuel(value: object) -> str | None:
    if pd.isna(value):
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:1].upper() + s[1:].lower() if s.isalpha() else s.title()


def _digits_only(value: object) -> str | None:
    if pd.isna(value):
        return None
    s = re.sub(r"\D", "", str(value))
    return s or None


# cache_resource, not cache_data: cache_data hands every caller a fresh
# pickled COPY of the return value, which at 1.1M rows costs seconds per
# widget interaction. The cached frames are treated as read-only.
@st.cache_resource(show_spinner="Loading FRS city centroids…")
def load_city_centroids(csv_path: Path, parquet_path: Path = CENTROIDS_PARQUET) -> pd.DataFrame:
    """Median lat/lon per (state, city) from EPA FRS, used to plot facility points.

    Source preference:
      1. ``PERMIT_DASHBOARD_S3_URI/city_centroids.parquet`` (if env var set)
      2. local prebuilt parquet (~1 MB)
      3. raw 336 MB FRS CSV (slow fallback)
    """
    remote_uri = _s3_uri_for(CENTROIDS_FILENAME)
    if remote_uri is not None:
        return _read_remote_parquet(remote_uri)
    if parquet_path.exists():
        return pd.read_parquet(parquet_path)
    if not csv_path.exists():
        return pd.DataFrame(columns=["key_state", "key_city", "Lat", "Lon"])
    frs = pd.read_csv(
        csv_path,
        dtype=str,
        usecols=["FAC_CITY", "FAC_STATE", "LATITUDE_MEASURE", "LONGITUDE_MEASURE"],
    )
    frs["Lat"] = pd.to_numeric(frs["LATITUDE_MEASURE"], errors="coerce")
    frs["Lon"] = pd.to_numeric(frs["LONGITUDE_MEASURE"], errors="coerce")
    frs = frs.dropna(subset=["Lat", "Lon", "FAC_CITY", "FAC_STATE"])
    frs = frs[
        frs["Lat"].between(17, 72)
        & frs["Lon"].between(-180, -65)
        & (frs["Lat"] != 0)
    ]
    frs["key_state"] = frs["FAC_STATE"].str.upper().str.strip()
    frs["key_city"] = frs["FAC_CITY"].str.upper().str.strip()
    centroids = (
        frs.groupby(["key_state", "key_city"])[["Lat", "Lon"]].median().reset_index()
    )
    return centroids


# Lat/Lon are intentionally excluded: they come from the centroid merge, which
# still runs at load time so that one published parquet works with both this
# code and older deployments (a file carrying Lat/Lon would make the old merge
# emit Lat_x/Lat_y and drop plain "Lat").
_DERIVED_COLUMNS = frozenset(
    ["NAICS_clean", "Subsector", "Site Key", "Capacity Value (num)",
     "Capacity Unit (norm)", "Fuel Type (norm)", "Unit Quantity (num)",
     "key_state", "key_city"]
    + [f"is_{code}" for code in KEYWORD_FILTERS]
)


@st.cache_resource(show_spinner="Loading permit dataset…")
def load_data(
    xlsx_path: Path,
    centroids_path: Path,
    parquet_path: Path = PERMITS_PARQUET,
) -> pd.DataFrame:
    remote_uri = _s3_uri_for(PERMITS_FILENAME)
    if remote_uri is not None:
        df = _read_remote_parquet(remote_uri)
        for col in ("NAICS Code", "Classified NAICS"):
            if col in df.columns:
                df[col] = df[col].astype("string")
    elif parquet_path.exists():
        df = _parquet_to_df(parquet_path)
        # Excel was loaded as str; ensure parquet path matches by coercing
        # any non-string columns we treat as text.
        for col in ("NAICS Code", "Classified NAICS"):
            if col in df.columns:
                df[col] = df[col].astype("string")
    else:
        sheets = []
        for sheet in SHEET_NAMES:
            try:
                s = pd.read_excel(xlsx_path, sheet_name=sheet, dtype=str)
            except ValueError:
                continue
            s["Source Sheet"] = sheet
            sheets.append(s)
        if not sheets:
            return pd.DataFrame()
        df = pd.concat(sheets, ignore_index=True)

    # A dashboard-ready parquet (scripts/build_dashboard_ready_parquet.py) already
    # carries every derived column and the centroid join. Recomputing them here
    # exhausts Streamlit's memory budget at current row counts, so short-circuit.
    if _DERIVED_COLUMNS.issubset(df.columns):
        centroids = load_city_centroids(centroids_path)
        return df.merge(centroids, on=["key_state", "key_city"], how="left")

    # Pick the most reliable NAICS: prefer Classified NAICS (full 6-digit),
    # fall back to the raw NAICS Code from the permit. The "Other NAICS" sheet
    # has no Classified NAICS column, so handle that gracefully.
    classified = (
        df["Classified NAICS"].apply(_digits_only)
        if "Classified NAICS" in df.columns
        else pd.Series([None] * len(df), index=df.index)
    )
    raw = df["NAICS Code"].apply(_digits_only)
    naics = classified.where(classified.notna(), raw)
    df["NAICS_clean"] = naics
    df["Subsector"] = naics.str[:3]

    # Tag rows that match keyword-based virtual subsectors (data center,
    # water, wastewater). A row can match multiple — store as a set per row
    # for flexible filtering.
    text_cols = []
    for c in ("Industry Description", "Facility Name"):
        if c in df.columns:
            text_cols.append(df[c].fillna(""))
    haystack = (
        text_cols[0].str.cat(text_cols[1:], sep=" ")
        if text_cols
        else pd.Series([""] * len(df), index=df.index)
    )
    for code, pattern in KEYWORD_FILTERS.items():
        df[f"is_{code}"] = haystack.str.contains(pattern)

    # Site key: facility + city + state (best available stable identifier).
    df["Site Key"] = (
        df["Facility Name"].fillna("").str.strip().str.upper()
        + " | "
        + df["Facility City"].fillna("").str.strip().str.upper()
        + " | "
        + df["Facility State Abbreviation"].fillna("").str.strip().str.upper()
    )

    # Numeric capacity (best-effort) and normalized unit.
    df["Capacity Value (num)"] = pd.to_numeric(df["Capacity Value"], errors="coerce")
    df["Capacity Unit (norm)"] = df["Capacity Unit"].apply(_normalize_capacity_unit)
    df["Fuel Type (norm)"] = df["Fuel Type"].apply(_normalize_fuel)
    df["Unit Quantity (num)"] = pd.to_numeric(df["Unit Quantity"], errors="coerce")
    if "Industry Description" in df.columns:
        df["Industry Description (norm)"] = df["Industry Description"].apply(
            lambda v: _normalize_freetext(v, INDUSTRY_SYNONYMS))
    if "Unit Type" in df.columns:
        df["Unit Type (norm)"] = df["Unit Type"].apply(
            lambda v: _normalize_freetext(v, UNIT_TYPE_SYNONYMS))

    # Attach city-centroid coordinates for points-on-map view.
    centroids = load_city_centroids(centroids_path)
    df["key_state"] = df["Facility State Abbreviation"].str.upper().str.strip()
    df["key_city"] = df["Facility City"].str.upper().str.strip()
    df = df.merge(centroids, on=["key_state", "key_city"], how="left")

    return df


def _mfg_mask(df: pd.DataFrame) -> pd.Series:
    """Rows in the manufacturing scope (3-digit NAICS 311-339)."""
    return df["Subsector"].isin(NAICS_SUBSECTOR_LABELS)


def filter_by_option(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """Apply the dropdown selection to the dataframe."""
    if code == ALL_CODE:
        return df
    if code == MFG_CODE:
        return df[_mfg_mask(df)]
    if code in KEYWORD_FILTERS:
        return df[df[f"is_{code}"]]
    return df[df["Subsector"] == code]


def option_row_count(df: pd.DataFrame, code: str) -> int:
    if code == ALL_CODE:
        return len(df)
    if code == MFG_CODE:
        return int(_mfg_mask(df).sum())
    if code in KEYWORD_FILTERS:
        return int(df[f"is_{code}"].sum())
    return int((df["Subsector"] == code).sum())


# Subsectors with fewer rows than this are left out of the dropdown (they are
# still counted in the entire-dataset view and in the pie's "Other" slice).
MIN_SUBSECTOR_ROWS = 100


def subsector_options(df: pd.DataFrame) -> list[tuple[str, str]]:
    """Return (code, label) pairs for the dropdown.

    Order: entire dataset → manufacturing aggregate → keyword virtual
    subsectors → every 3-digit subsector present (largest first). Each label
    includes the row count for the option.
    """
    items: list[tuple[str, str]] = []

    items.append((ALL_CODE, f"Entire dataset  ({len(df):,} units)"))
    items.append((MFG_CODE,
                  f"Manufacturing (NAICS 31–33)  ({option_row_count(df, MFG_CODE):,} units)"))

    virtual = [
        (DATA_CENTER_CODE, "Data centers"),
        (WATER_CODE, "Water treatment / supply"),
        (WASTEWATER_CODE, "Wastewater / sewage"),
    ]
    for code, label in virtual:
        items.append((code, f"{label}  ({option_row_count(df, code):,} units)"))

    counts = df["Subsector"].value_counts()
    for code, count in counts.items():
        if not isinstance(code, str) or not code:
            continue
        if count < MIN_SUBSECTOR_ROWS:
            break  # value_counts is sorted descending
        items.append((code, f"{code} — {subsector_label(code)}  ({count:,} units)"))
    return items


def _load_df(data_path_str: str) -> pd.DataFrame:
    return load_data(Path(data_path_str), FRS_FACILITIES_PATH)


@st.cache_resource(max_entries=4, show_spinner=False)
def get_subset(code: str, states: tuple, data_path_str: str) -> pd.DataFrame:
    """Filtered view of the dataset, shared (not copied) across reruns."""
    sub = filter_by_option(_load_df(data_path_str), code)
    if states:
        sub = sub[sub["Facility State Abbreviation"].isin(states)]
    return sub


@st.cache_data(show_spinner=False)
def cached_options(data_path_str: str) -> list[tuple[str, str]]:
    return subsector_options(_load_df(data_path_str))


def _pref(sub: pd.DataFrame, col: str) -> str:
    """Prefer the normalized variant of a column when the file carries it."""
    norm = f"{col} (norm)"
    return norm if norm in sub.columns else col


_YEAR_RE = re.compile(r"(19[0-9]{2}|20[0-2][0-9])")

# Placeholder strings that masquerade as makes/models.
_PLACEHOLDER_VALUES = {
    "unknown", "n/a", "na", "none", "not specified", "not available",
    "not listed", "unspecified", "various", "-", "--", "unk", "tbd",
}


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDER_VALUES


def _year_counts(series: pd.Series) -> pd.DataFrame:
    """Units per year of manufacture. Parses the year out of each *unique*
    label (labels include '1998', '1998.0', '~1995', 'circa 1987'), so the
    cost scales with distinct values, not rows."""
    vc = series.dropna().value_counts()
    vc = vc[vc > 0]
    years: dict[int, int] = {}
    for label, cnt in vc.items():
        m = _YEAR_RE.search(str(label))
        if m:
            y = int(m.group(1))
            if y > 2026:  # OCR/typo future years
                continue
            years[y] = years.get(y, 0) + int(cnt)
    if not years:
        return pd.DataFrame(columns=["year", "units"])
    return pd.DataFrame(sorted(years.items()), columns=["year", "units"])


def _make_model_counts(sub: pd.DataFrame, n: int) -> pd.DataFrame:
    """Top make+model pairs ('Caterpillar 3512' — a bare model number is
    meaningless without its make)."""
    make_col, model_col = _pref(sub, "Unit Make"), "Unit Model"
    if make_col not in sub.columns or model_col not in sub.columns:
        return pd.DataFrame(columns=["value", "count"])
    pairs = (
        sub.dropna(subset=[make_col, model_col])
        .groupby([make_col, model_col], observed=True)
        .size()
        .sort_values(ascending=False)
    )
    rows = []
    for (make, model), cnt in pairs.items():
        make_s, model_s = str(make).strip(), str(model).strip()
        if not make_s or not model_s:
            continue
        if _is_placeholder(make_s) or _is_placeholder(model_s):
            continue
        rows.append((f"{make_s} {model_s}", int(cnt)))
        if len(rows) == n:
            break
    return pd.DataFrame(rows, columns=["value", "count"])


@st.cache_data(max_entries=12, show_spinner="Crunching view…")
def view_aggregates(code: str, states: tuple, data_path_str: str) -> dict:
    """Everything the charts need for one (subsector, states) selection,
    computed in a single pass and cached. The heavy per-rerun groupbys were
    what made the app crawl at 1.1M rows."""
    sub = get_subset(code, states, data_path_str)
    out: dict = {"n": len(sub)}

    # observed=True everywhere: Site Key is categorical, and the default
    # (observed=False) emits a zero-size group for every category absent
    # from the filtered view — wrong medians and a 33k-row site table.
    ups = sub.groupby("Site Key", observed=True).size()
    out["kpi"] = {
        "facilities": int(sub["Site Key"].nunique()),
        "units": len(sub),
        "states": int(sub["Facility State Abbreviation"].nunique()),
        "median_units": float(ups.median()) if len(ups) else 0.0,
        "permits": int(sub["Permit Number"].nunique()) if "Permit Number" in sub.columns else 0,
        "documents": int(sub["Filename"].nunique()) if "Filename" in sub.columns else 0,
    }
    out["ups"] = ups.rename("units").reset_index()

    if code in (ALL_CODE, MFG_CODE):
        bucket = _primary_bucket(sub).fillna("Unclassified")
        vc = bucket.value_counts()
        if len(vc) > 14:
            vc = pd.concat([vc.head(13),
                            pd.Series({"Other subsectors": int(vc.iloc[13:].sum())})])
        out["pie"] = vc.rename_axis("Subsector").reset_index(name="units")
    else:
        out["pie"] = None

    out["sites"] = (
        sub.groupby("Site Key", observed=True)
        .agg(facility=("Facility Name", "first"),
             city=("Facility City", "first"),
             state=("Facility State Abbreviation", "first"),
             units=("Site Key", "size"),
             lat=("Lat", "first"),
             lon=("Lon", "first"))
        .reset_index(drop=True)
    )

    out["top_unit_type"] = _top_counts(sub[_pref(sub, "Unit Type")], 15)
    out["top_fuel"] = _top_counts(sub[_pref(sub, "Fuel Type")], 15)
    out["top_control"] = _top_counts(sub[_pref(sub, "Control Device(s)")], 15)
    out["top_industry"] = _top_counts(sub[_pref(sub, "Industry Description")], 15)

    # 'None' is meaningful for control devices but noise for makes.
    top_make = _top_counts(sub[_pref(sub, "Unit Make")], 25)
    out["top_make"] = top_make[~top_make["value"].map(_is_placeholder)].head(15)
    out["top_model"] = _make_model_counts(sub, 15)
    out["vintage"] = _year_counts(sub["Year of Manufacture"]) \
        if "Year of Manufacture" in sub.columns else pd.DataFrame(columns=["year", "units"])

    out["cap"] = sub[["Capacity Value (num)", "Capacity Unit (norm)"]].dropna()

    cols = export_columns(sub)
    out["export_cols"] = cols
    out["table_head"] = sub[cols].head(500)
    return out


def option_display_name(code: str) -> str:
    if code == ALL_CODE:
        return "Entire dataset"
    if code == MFG_CODE:
        return "Manufacturing (NAICS 31–33)"
    if code == DATA_CENTER_CODE:
        return "Data centers"
    if code == WATER_CODE:
        return "Water treatment / supply"
    if code == WASTEWATER_CODE:
        return "Wastewater / sewage"
    return f"{code} — {subsector_label(code)}"


def kpi_row(k: dict) -> None:
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Facilities (sites)", f"{k['facilities']:,}")
    c2.metric("Permits", f"{k['permits']:,}")
    c3.metric("Permit documents", f"{k['documents']:,}")
    c4.metric("Emission units", f"{k['units']:,}")
    c5.metric("States represented", f"{k['states']}")
    c6.metric("Median units / site", f"{k['median_units']:.0f}")


def _primary_bucket(sub: pd.DataFrame) -> pd.Series:
    """One subsector label per row. Keyword-tagged rows take precedence over
    the NAICS 3-digit bucket so a manufacturing-classified backup-generator at
    a data center gets counted as 'Data centers'.
    """
    label = pd.Series(index=sub.index, dtype="object")
    # NAICS bucket first (lowest priority).
    naics_label = sub["Subsector"].map(
        lambda c: f"{c} — {subsector_label(c)}" if isinstance(c, str) and c else None
    )
    label = label.where(label.notna(), naics_label)
    # Keyword tags overwrite (highest priority: data center, then water, then wastewater).
    for code, name in (
        (WASTEWATER_CODE, "Wastewater / sewage"),
        (WATER_CODE, "Water treatment / supply"),
        (DATA_CENTER_CODE, "Data centers"),
    ):
        flag = sub[f"is_{code}"]
        label = label.where(~flag, name)
    return label


def chart_subsector_pie(counts: pd.DataFrame) -> None:
    """Pie of subsector composition for the aggregate scopes."""
    if counts is None or counts.empty:
        st.info("No rows to summarize.")
        return
    fig = px.pie(
        counts,
        names="Subsector",
        values="units",
        hole=0.35,
    )
    fig.update_traces(
        textposition="inside",
        textinfo="percent+label",
        sort=True,
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=460, showlegend=True)
    st.plotly_chart(fig, use_container_width=True)


MAX_MAP_POINTS = 8000


def chart_state_distribution(sites: pd.DataFrame, uniform_markers: bool = False) -> None:
    plotted = sites.dropna(subset=["lat", "lon"])
    capped = len(plotted) > MAX_MAP_POINTS
    if capped:
        # Browser-side rendering chokes on tens of thousands of markers;
        # keep the largest sites, which carry the visual story anyway.
        plotted = plotted.nlargest(MAX_MAP_POINTS, "units")

    map_col, bar_col = st.columns([1.4, 1])
    with map_col:
        if plotted.empty:
            st.info("No facilities resolved to coordinates for this subsector.")
        else:
            if uniform_markers:
                fig_map = px.scatter_geo(
                    plotted,
                    lat="lat",
                    lon="lon",
                    scope="usa",
                    hover_name="facility",
                    hover_data={
                        "city": True,
                        "state": True,
                        "units": True,
                        "lat": False,
                        "lon": False,
                    },
                    opacity=0.55,
                )
                fig_map.update_traces(
                    marker=dict(size=4, color="#1f77b4", line=dict(width=0))
                )
            else:
                fig_map = px.scatter_geo(
                    plotted,
                    lat="lat",
                    lon="lon",
                    scope="usa",
                    size="units",
                    size_max=22,
                    color="units",
                    color_continuous_scale="Viridis",
                    hover_name="facility",
                    hover_data={
                        "city": True,
                        "state": True,
                        "units": True,
                        "lat": False,
                        "lon": False,
                    },
                    opacity=0.7,
                )
                fig_map.update_traces(marker=dict(line=dict(width=0)))
            fig_map.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=420)
            st.plotly_chart(fig_map, use_container_width=True)
            missing = len(sites) - len(plotted)
            base_caption = (
                f"{len(plotted):,} of {len(sites):,} sites plotted"
                + (f" — showing the {MAX_MAP_POINTS:,} largest sites" if capped else "")
                + (f" — {missing:,} not geocoded or below the size cutoff" if missing else "")
                + ". Points are FRS city centroids; co-located facilities overlap."
            )
            st.caption(base_caption)

    with bar_col:
        by_state = (
            sites.dropna(subset=["state"])
            .groupby("state", observed=True)
            .agg(facilities=("facility", "nunique"), units=("units", "sum"))
            .reset_index()
            .sort_values("facilities", ascending=False)
        )
        if by_state.empty:
            st.info("No state-level data available.")
            return
        top = by_state.head(15).iloc[::-1]
        fig_bar = px.bar(
            top,
            x="facilities",
            y="state",
            orientation="h",
            labels={"facilities": "Facilities", "state": "State"},
            hover_data={"units": True},
        )
        fig_bar.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=420)
        st.plotly_chart(fig_bar, use_container_width=True)


def chart_units_per_site(units_per_site: pd.DataFrame) -> None:
    if units_per_site.empty:
        return
    cap = int(units_per_site["units"].quantile(0.99))
    cap = max(cap, 5)
    fig = px.histogram(
        units_per_site,
        x="units",
        nbins=min(50, cap),
        range_x=(0, cap),
        labels={"units": "Emission units per site"},
    )
    p50 = int(units_per_site["units"].median())
    p90 = int(units_per_site["units"].quantile(0.9))
    fig.add_vline(x=p50, line_dash="dash", annotation_text=f"median={p50}")
    fig.add_vline(x=p90, line_dash="dot", annotation_text=f"p90={p90}")
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=320, bargap=0.05)
    st.plotly_chart(fig, use_container_width=True)


def _top_counts(series: pd.Series, n: int) -> pd.DataFrame:
    # Count first, clean after: value_counts on a categorical is near-free,
    # while .astype(str).str.strip() on the full column materializes millions
    # of Python strings (measured: ~47 s and gigabytes at 1.1M rows).
    vc = series.dropna().value_counts()
    vc = vc[vc > 0]
    if vc.empty:
        return pd.DataFrame(columns=["value", "count"])
    labels = vc.index.astype(str).str.strip()
    vc = pd.Series(vc.values, index=labels)
    vc = vc[vc.index != ""]
    if vc.empty:
        return pd.DataFrame(columns=["value", "count"])
    vc = vc.groupby(level=0).sum().sort_values(ascending=False)
    return vc.head(n).rename_axis("value").reset_index(name="count")


def chart_top_categorical(top: pd.DataFrame, title: str) -> None:
    if top.empty:
        st.info(f"No data for {title.lower()}.")
        return
    fig = px.bar(
        top.iloc[::-1],
        x="count",
        y="value",
        orientation="h",
        labels={"value": title, "count": "Units"},
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=380)
    st.plotly_chart(fig, use_container_width=True)


def chart_vintage(vintage: pd.DataFrame) -> None:
    if vintage.empty:
        st.info("No year-of-manufacture data populated for this selection.")
        return
    v = vintage[vintage["year"] >= 1940]
    total = int(v["units"].sum())
    if total == 0:
        st.info("No year-of-manufacture data populated for this selection.")
        return
    cum = v["units"].cumsum()
    median_year = int(v.loc[cum >= total / 2, "year"].iloc[0])
    fig = px.bar(v, x="year", y="units",
                 labels={"year": "Year of manufacture", "units": "Units"})
    fig.add_vline(x=median_year, line_dash="dash",
                  annotation_text=f"median {median_year}")
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=320, bargap=0.05)
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        f"{total:,} units state a year of manufacture (pre-1940 omitted). "
        f"Median vintage: {median_year}."
    )


def chart_capacity_distribution(cap: pd.DataFrame) -> None:
    if cap.empty:
        st.info("No capacity data populated for this subsector.")
        return

    unit_counts = cap["Capacity Unit (norm)"].value_counts()
    common_units = unit_counts.head(8).index.tolist()
    selected = st.multiselect(
        "Capacity units to compare",
        options=common_units,
        default=common_units[: min(4, len(common_units))],
        help="Capacity is reported in many units; pick a comparable set.",
    )
    plot_df = cap[cap["Capacity Unit (norm)"].isin(selected)]
    if plot_df.empty:
        st.info("Pick at least one capacity unit to plot.")
        return
    plot_df = plot_df[plot_df["Capacity Value (num)"] > 0]
    fig = px.box(
        plot_df,
        x="Capacity Unit (norm)",
        y="Capacity Value (num)",
        log_y=True,
        labels={
            "Capacity Unit (norm)": "Capacity unit",
            "Capacity Value (num)": "Capacity (log scale)",
        },
        points="suspectedoutliers",
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=360)
    st.plotly_chart(fig, use_container_width=True)


# Internal plumbing columns kept out of the table and the export files.
_EXPORT_EXCLUDE = frozenset(
    ["Lat", "Lon", "key_state", "key_city", "Site Key"]
    + [f"is_{code}" for code in KEYWORD_FILTERS]
)

# Reader-friendly ordering for the front of the table; every remaining source
# column follows in file order so the full schema is always visible.
_LEAD_COLUMNS = [
    "Facility Name", "Facility City", "Facility State Abbreviation",
    "NAICS_clean", "NAICS Source", "Industry Description",
    "Unit ID", "Unit Description", "Unit Type", "Unit Make", "Unit Model",
    "Year of Manufacture", "Capacity Value", "Capacity Unit", "Fuel Type",
    "Pollutants", "Emission Limits", "Control Device(s)",
    "Permit Number", "Issuance Date", "Expiration Date",
    "Regulatory Authority", "Filename",
]


def export_columns(sub: pd.DataFrame) -> list[str]:
    lead = [c for c in _LEAD_COLUMNS if c in sub.columns]
    rest = [c for c in sub.columns if c not in lead and c not in _EXPORT_EXCLUDE]
    return lead + rest


def render_data_table(table_head: pd.DataFrame, total_rows: int, n_cols: int) -> None:
    st.dataframe(table_head, use_container_width=True, height=380)
    st.caption(
        f"Showing first 500 of {total_rows:,} rows — all {n_cols} data columns. "
        "Use the download buttons below for the complete selection."
    )


# CSV blows up to ~3 KB/row with every column included; cap it so a download
# click can't exhaust the server's memory. Parquet has no cap.
CSV_MAX_ROWS = 250_000


def render_downloads(code: str, state_filter: list[str], data_path_str: str,
                     n: int, cols: list[str]) -> None:
    st.markdown("### Download this selection")
    slug = "permit_units_" + (code.lower() if code else "all")
    if state_filter:
        slug += "_" + "-".join(s.lower() for s in sorted(state_filter)[:5])

    prepare = st.checkbox(
        f"Prepare download files ({n:,} rows × {len(cols)} columns)",
        help="Files are generated on demand so browsing stays fast.",
    )
    if not prepare:
        return
    sub = get_subset(code, tuple(state_filter), data_path_str)
    export = sub[cols].copy()
    # Filtered frames inherit the full dataset's category dictionaries, which
    # bloats the written file; drop the unused entries first.
    for c in export.select_dtypes(include="category").columns:
        export[c] = export[c].cat.remove_unused_categories()

    buf = io.BytesIO()
    export.to_parquet(buf, index=False)
    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "Download Parquet (recommended)",
            data=buf.getvalue(),
            file_name=f"{slug}.parquet",
            mime="application/octet-stream",
            use_container_width=True,
        )
    with c2:
        if n <= CSV_MAX_ROWS:
            csv_bytes = export.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "Download CSV",
                data=csv_bytes,
                file_name=f"{slug}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        else:
            st.caption(
                f"CSV export is capped at {CSV_MAX_ROWS:,} rows; narrow the "
                "selection with a subsector or state filter, or use the "
                "Parquet file (readable in pandas, R, Excel via Power Query)."
            )


def main() -> None:
    st.set_page_config(
        page_title="Permit Subsector Dashboard",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("Permit Subsector Dashboard")

    with st.sidebar:
        st.header("Data")
        remote_uri = _s3_uri_for(PERMITS_FILENAME)
        if remote_uri is not None:
            st.caption(f"Loading from `{remote_uri}`")
        elif PERMITS_PARQUET.exists():
            st.caption(f"Loading from `{PERMITS_PARQUET.relative_to(PROJECT_ROOT)}`")
        else:
            st.warning(
                "No prebuilt parquet found — falling back to xlsx (slow). "
                + BUILD_HINT
            )
        data_path_str = st.text_input(
            "Source xlsx (used if parquet missing)",
            value=str(DEFAULT_DATA_PATH),
            help="Path to permit_data_extracted.xlsx",
        )
        data_path = Path(data_path_str)
        have_local_parquet = PERMITS_PARQUET.exists()
        if not (remote_uri or have_local_parquet or data_path.exists()):
            st.error(f"Neither remote URI, local parquet, nor xlsx available. {BUILD_HINT}")
            st.stop()

    options = cached_options(data_path_str)
    if not options:
        st.error("No subsector rows found in the dataset.")
        st.stop()

    with st.sidebar:
        st.header("Subsector")
        labels = [label for _, label in options]
        codes = [code for code, _ in options]
        choice = st.selectbox("Subsector", labels, index=0)
        chosen_code = codes[labels.index(choice)]

        st.header("Filters")
        df = _load_df(data_path_str)
        states_available = sorted(
            set(df["Facility State Abbreviation"].dropna().unique())
            & VALID_STATE_CODES
        )
        state_filter = st.multiselect("States", options=states_available, default=[])

    view = view_aggregates(chosen_code, tuple(state_filter), data_path_str)

    st.subheader(option_display_name(chosen_code))
    if chosen_code == ALL_CODE:
        st.caption(
            "Every unit-level record extracted from state and district air "
            "permits. Select a subsector in the sidebar to drill down."
        )
    if view["n"] == 0:
        st.warning("No rows match the current filters.")
        return

    kpi_row(view["kpi"])

    if view["pie"] is not None:
        st.markdown("### Subsector composition")
        chart_subsector_pie(view["pie"])

    st.markdown("### Geographic distribution")
    chart_state_distribution(view["sites"],
                             uniform_markers=(chosen_code in (ALL_CODE, MFG_CODE)))

    st.markdown("### Units per site")
    chart_units_per_site(view["ups"])

    left, right = st.columns(2)
    with left:
        st.markdown("### Top unit types")
        chart_top_categorical(view["top_unit_type"], "Unit type")
    with right:
        st.markdown("### Top fuel types")
        chart_top_categorical(view["top_fuel"], "Fuel type")

    left, right = st.columns(2)
    with left:
        st.markdown("### Top manufacturers")
        chart_top_categorical(view["top_make"], "Manufacturer")
    with right:
        st.markdown("### Top make + model")
        chart_top_categorical(view["top_model"], "Make and model")

    st.markdown("### Equipment vintage")
    chart_vintage(view["vintage"])

    left, right = st.columns(2)
    with left:
        st.markdown("### Top control devices")
        chart_top_categorical(view["top_control"], "Control device")
    with right:
        st.markdown("### Top industry descriptions")
        chart_top_categorical(view["top_industry"], "Industry description")

    st.markdown("### Capacity distribution by reported unit")
    chart_capacity_distribution(view["cap"])

    st.markdown("### Data table (all columns)")
    render_data_table(view["table_head"], view["n"], len(view["export_cols"]))

    render_downloads(chosen_code, state_filter, data_path_str,
                     view["n"], view["export_cols"])


if __name__ == "__main__":
    main()
