"""Build the most-complete-and-recent dataset: the UNION of the April manuscript
extraction and the May dashboard extraction.

Background (see memory `manuscript-vs-dashboard-data-mismatch`): neither existing
file is a superset of the other. Normalized by filename, April has 16,431 permits,
May has 21,424, only 9,625 are shared -> union = 28,230 permits. May is more recent
and cleaner (100% ISO dates, better Primary Applicable Regulations, populated
Classified NAICS) but dropped three fields that April populated:
Model Used (89%->0%), Annual Run Hours (13%->0%), Rated Efficiency (5%->0%).

Merge strategy (permit-level source selection — rows are unit-level and the two
runs segment units differently, so we never interleave unit rows for one permit):
  * May permits      -> keep May rows (cleaner / more recent).
  * April-only permits -> append April rows (recovers the 6,806 May lacks).
  * Model Used is a per-file attribute: backfill onto shared May permits from
    April; April-sourced rows keep theirs; otherwise tagged "unknown (may run)".
  * Annual Run Hours / Rated Efficiency survive on April-sourced rows only; left
    blank where only May data exists (no fabricated per-unit join).
  * A `Source Run` column records which extraction each row came from.

Output: data/processed/permit_data_union.csv
"""

from pathlib import Path
import re

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
APRIL = ROOT / "data" / "processed" / "permit_data_full.csv"
MAY_XLSX = ROOT / "data" / "processed" / "permit_data_extracted.xlsx"
OUT = ROOT / "data" / "processed" / "permit_data_union.csv"
SHEETS = ("Manufacturing NAICS 31-33", "Other NAICS")

APRIL_RUN = "april_2026-04-15"
MAY_RUN = "may_2026-05-05"

# May verbose extraction header -> canonical short name (from build_release_from_dashboard).
MAY_RENAME = {
    "Model Used (AI extraction model)": "Model Used",
    "Owner/Operator Name (if different from Facility Name)": "Owner/Operator Name",
    "SIC Code (Standard Industrial Classification)": "SIC Code",
    "Operating Hours (facility-wide schedule)": "Operating Hours",
    "Opacity Limit (visible emission limit for this unit)": "Opacity Limit",
    "Throughput/Production Limit (material processing limits)": "Throughput/Production Limit",
    "Rated Efficiency (control/destruction/capture or equipment thermal — as stated)": "Rated Efficiency",
    "Annual Run Hours (this emission unit)": "Annual Run Hours",
    "Applicable NESHAP/NSPS Subpart (federal standard per unit)": "Applicable NESHAP/NSPS Subpart",
    "Permit Type (e.g. Title V, State Only, Synthetic Minor)": "Permit Type",
}

# April parsed-hours headers -> May-style canonical names so the columns align.
APRIL_RENAME = {
    "Operating Hours Value": "Operating Hours — numeric/value portion",
    "Operating Hours Time Basis": "Operating Hours — unit or basis (e.g. hours/year)",
    "Annual Run Hours Value": "Annual Run Hours — numeric/value portion",
    "Annual Run Hours Time Basis": "Annual Run Hours — unit or basis (e.g. hours/year)",
}

_EMPTY = {"", "nan", "none", "null"}


def norm_fn(s) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"\.(pdf|txt|json|html?)$", "", s)
    return s


def is_blank(series: pd.Series) -> pd.Series:
    return series.isna() | series.astype(str).str.strip().str.lower().isin(_EMPTY)


def main() -> None:
    april = pd.read_csv(APRIL, dtype=str, low_memory=False).rename(columns=APRIL_RENAME)
    may = pd.concat(
        [pd.read_excel(MAY_XLSX, sheet_name=s, dtype=str) for s in SHEETS],
        ignore_index=True,
    ).rename(columns=MAY_RENAME)
    print(f"  [load] april {len(april):,} rows / {april['Filename'].nunique():,} permits / {april.shape[1]} cols")
    print(f"  [load] may   {len(may):,} rows / {may['Filename'].nunique():,} permits / {may.shape[1]} cols")

    april["_fn"] = april["Filename"].map(norm_fn)
    may["_fn"] = may["Filename"].map(norm_fn)
    april_keys = set(april["_fn"])
    may_keys = set(may["_fn"])
    shared = april_keys & may_keys
    april_only_keys = april_keys - may_keys
    print(f"  [keys] shared={len(shared):,} april_only={len(april_only_keys):,} may_only={len(may_keys - april_keys):,} union={len(april_keys | may_keys):,}")

    # Per-file Model Used from April (first non-blank value per filename).
    av = april[~is_blank(april["Model Used"])] if "Model Used" in april else april.iloc[0:0]
    april_model = av.groupby("_fn")["Model Used"].first().to_dict() if len(av) else {}

    # Canonical column order: union, April schema first then May extras.
    cols = list(april.columns)
    for c in may.columns:
        if c not in cols:
            cols.append(c)
    cols = [c for c in cols if c != "_fn"]

    # May rows (all May permits), with Model Used backfilled from April per file.
    may_out = may.reindex(columns=cols + ["_fn"])
    bf = may_out["_fn"].map(april_model)
    blank_model = is_blank(may_out["Model Used"])
    may_out.loc[blank_model, "Model Used"] = bf[blank_model]
    still_blank = is_blank(may_out["Model Used"])
    may_out.loc[still_blank, "Model Used"] = "unknown (may run)"
    may_out["Source Run"] = MAY_RUN

    # April-only rows.
    april_only = april[april["_fn"].isin(april_only_keys)].reindex(columns=cols + ["_fn"])
    april_only["Source Run"] = APRIL_RUN

    union = pd.concat([may_out, april_only], ignore_index=True).drop(columns=["_fn"])
    union.to_csv(OUT, index=False)

    print(f"\n  [write] {OUT}")
    print(f"          {len(union):,} rows / {union['Filename'].nunique():,} permits / {union.shape[1]} cols")
    print(f"          source: may={ (union['Source Run']==MAY_RUN).sum():,} rows  april-only={ (union['Source Run']==APRIL_RUN).sum():,} rows")
    for f in ["Model Used", "Annual Run Hours", "Rated Efficiency", "Issuance Date", "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)"]:
        if f in union:
            print(f"          {f[:45]:45s} populated {(~is_blank(union[f])).mean()*100:5.1f}%")


if __name__ == "__main__":
    main()
