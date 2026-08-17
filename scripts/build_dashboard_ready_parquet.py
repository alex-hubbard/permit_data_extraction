"""Build a dashboard-ready parquet: slim columns + precomputed derived fields.

The Streamlit app previously did all of its derivation at load time --- several
full-column .apply() passes, three regex scans, a string concat, and a merge ---
on every cold start. That fit in Streamlit's memory budget at ~282K rows but not
at the current ~1.1M, where the app OOMs.

This moves that work into the build. It keeps only the columns the app reads,
precomputes every derived column the app would otherwise compute, and writes
dictionary-encoded parquet. The app then loads a frame it can use directly
(see the `_DERIVED_PRESENT` short-circuit in load_data).

Derivation logic is imported from the dashboard module so the two cannot drift.

Usage:
    PYTHONPATH=. python scripts/build_dashboard_ready_parquet.py \
        --in data/processed/dashboard/permits_v5a.parquet \
        --out data/processed/dashboard/permits_v5a_ready.parquet
"""
import argparse
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "dashboards")):
    if p not in sys.path:
        sys.path.insert(0, p)

import manufacturing_subsector as dash  # noqa: E402

# Source columns the app reads, plus what the derived columns are built from.
KEEP = [
    "Facility Name", "Facility City", "Facility State Abbreviation",
    "NAICS Code", "Classified NAICS", "Industry Description",
    "Unit Description", "Unit Type", "Unit Quantity",
    "Capacity Value", "Capacity Unit", "Fuel Type", "Control Device(s)",
    "Permit Number", "Source Sheet",
    "Filename",  # join key for the resolved-NAICS table
]
BATCH = 100_000

# filename -> (resolved NAICS, source), from backfill_naics.py + classify_naics_llm.py
NAICS_FINAL = Path("data/processed/analysis/naics_final.csv")


def load_naics_final():
    if not NAICS_FINAL.exists():
        print(f"NOTE: {NAICS_FINAL} not found; falling back to in-row NAICS columns")
        return {}
    import csv
    out = {}
    with NAICS_FINAL.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["naics_final"]:
                out[r["filename"]] = (r["naics_final"], r["naics_source"])
    print(f"resolved NAICS table: {len(out):,} permits")
    return out


def build_canon_map(src_path, col, synonyms):
    """fold(value) -> canonical display string, majority-casing across the file.

    Pass 1 of the free-text cleanup: read one column, count every distinct
    raw value, and for each folded group pick the most frequent original
    casing as the display form. Curated synonyms override per folded key.
    """
    vc = pq.read_table(src_path, columns=[col]).column(0).to_pandas().value_counts()
    groups = {}
    for val, cnt in vc.items():
        f = dash._fold_label(val)
        if f is None:
            continue
        best = groups.get(f)
        if best is None or cnt > best[0]:
            display = pd.Series([val]).str.replace(r"\s+", " ", regex=True)[0]
            groups[f] = (cnt, display.strip().rstrip(".;,"))
    canon = {}
    for f, (cnt, disp) in groups.items():
        # If the majority spelling is fully lowercase, title-case it for
        # display ('natural gas' -> 'Natural Gas'); mixed-case winners are
        # kept verbatim so acronyms and brand casing survive.
        canon[f] = disp.title() if disp.islower() else disp
    canon.update(synonyms)
    print(f"canon map for {col!r}: {len(vc):,} raw -> {len(set(canon.values())):,} canonical")
    return canon


def apply_canon(series: pd.Series, canon: dict) -> pd.Series:
    return series.map(lambda v: canon.get(dash._fold_label(v)))


def derive(df: pd.DataFrame, naics_final=None, canon_maps=None) -> pd.DataFrame:
    # Prefer the resolved code (permit > ECHO > derived > SIC > LLM, with the
    # 339999 catch-all excluded). The old precedence took the pipeline's
    # Classified NAICS over the permit's own code, which agreed with it only
    # 43% of the time. Fall back to the in-row columns where unresolved.
    classified = (df["Classified NAICS"].apply(dash._digits_only)
                  if "Classified NAICS" in df.columns
                  else pd.Series([None] * len(df), index=df.index))
    raw = df["NAICS Code"].apply(dash._digits_only)
    naics = raw.where(raw.notna(), classified)
    if naics_final and "Filename" in df.columns:
        resolved = df["Filename"].map(lambda f: (naics_final.get(f) or (None, None))[0])
        df["NAICS Source"] = df["Filename"].map(
            lambda f: (naics_final.get(f) or (None, ""))[1])
        naics = resolved.where(resolved.notna(), naics)
    else:
        df["NAICS Source"] = ""
    df["NAICS_clean"] = naics
    df["Subsector"] = naics.str[:3]

    haystack = df["Industry Description"].fillna("").str.cat(
        df["Facility Name"].fillna(""), sep=" ")
    for code, pattern in dash.KEYWORD_FILTERS.items():
        df[f"is_{code}"] = haystack.str.contains(pattern)

    df["Site Key"] = (
        df["Facility Name"].fillna("").str.strip().str.upper() + " | "
        + df["Facility City"].fillna("").str.strip().str.upper() + " | "
        + df["Facility State Abbreviation"].fillna("").str.strip().str.upper())
    df["Capacity Value (num)"] = pd.to_numeric(df["Capacity Value"], errors="coerce")
    df["Capacity Unit (norm)"] = df["Capacity Unit"].apply(dash._normalize_capacity_unit)
    df["Fuel Type (norm)"] = df["Fuel Type"].apply(dash._normalize_fuel)
    df["Unit Quantity (num)"] = pd.to_numeric(df["Unit Quantity"], errors="coerce")
    if canon_maps:
        for col, canon in canon_maps.items():
            if col in df.columns:
                df[f"{col} (norm)"] = apply_canon(df[col], canon)
    df["key_state"] = df["Facility State Abbreviation"].str.upper().str.strip()
    df["key_city"] = df["Facility City"].str.upper().str.strip()
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--slim", action="store_true",
                    help="keep only the app's chart columns (legacy behavior); "
                         "default keeps every source column so the app can show "
                         "the full table and offer downloads")
    args = ap.parse_args()

    # Lat/Lon are deliberately NOT baked in. The app merges centroids at load
    # time, and a file that already carries Lat/Lon makes that merge produce
    # Lat_x/Lat_y, leaving no plain "Lat" -- which crashes any app version
    # still running the old load path. Keeping the merge at runtime lets one
    # published file serve both old and new app code. The merge is cheap next
    # to the .apply() passes this build removes.
    centroids = None

    naics_final = load_naics_final()
    src = pq.ParquetFile(args.src)
    wanted = KEEP if args.slim else list(src.schema_arrow.names)
    present = [c for c in wanted if c in src.schema_arrow.names]
    missing = [c for c in KEEP if c not in src.schema_arrow.names]
    if missing:
        print(f"NOTE: source lacks {missing}; those derived columns will be empty")

    canon_maps = {}
    for col, synonyms in dash.CANON_COLUMNS:
        if col in src.schema_arrow.names:
            canon_maps[col] = build_canon_map(args.src, col, synonyms)

    writer = None
    n = 0
    try:
        for batch in src.iter_batches(batch_size=BATCH, columns=present):
            df = derive(batch.to_pandas(), naics_final, canon_maps)
            if centroids is not None:
                df = df.merge(centroids, on=["key_state", "key_city"], how="left")
            table = pa.Table.from_pandas(df, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(
                    args.out, table.schema, compression="zstd",
                    use_dictionary=True, write_statistics=True)
            writer.write_table(table)
            n += len(df)
            print(f"  {n:,} rows", flush=True)
    finally:
        if writer is not None:
            writer.close()
    mb = args.out.stat().st_size / 1e6
    cols = pq.ParquetFile(args.out).schema_arrow.names
    print(f"-> {args.out}: {n:,} rows, {len(cols)} columns, {mb:.1f} MB")


if __name__ == "__main__":
    main()
