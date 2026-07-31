"""Automated QC checks for the released permit dataset (paper Section 4.3).

Runs LLM-free consistency checks against the consolidated dataset and writes
automated_qc_report.json / .md under data/processed/validation/.
"""

import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "processed" / "permit_data_full.csv"
OUT_DIR = ROOT / "data" / "processed" / "validation"

US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
    "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR VI GU AS MP".split()
)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")


def rate(num, den):
    return round(num / den, 4) if den else None


def main():
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    df = pd.read_csv(input_path, low_memory=False)
    n = len(df)
    success = df[df["Status"].astype(str).str.startswith("Success")]
    ns = len(success)

    report = {"input": str(input_path), "total_rows": n, "successful_rows": ns}
    report["status_distribution"] = df["Status"].value_counts().to_dict()

    # Date format checks (successful rows with a value)
    dates = {}
    for col in ("Issuance Date", "Expiration Date", "Processing Date"):
        vals = success[col].dropna().astype(str)
        vals = vals[vals.str.strip() != ""]
        dates[col] = {
            "n_nonnull": len(vals),
            "iso8601_rate": rate(vals.str.match(ISO_DATE_RE).sum(), len(vals)),
        }
    report["date_format"] = dates

    # State code validity
    st = success["Facility State Abbreviation"].dropna().astype(str).str.strip()
    st = st[st != ""]
    invalid_states = st[~st.isin(US_STATES)]
    report["state_codes"] = {
        "n_nonnull": len(st),
        "valid_rate": rate(len(st) - len(invalid_states), len(st)),
        "invalid_values": invalid_states.value_counts().head(20).to_dict(),
    }

    # ZIP validity
    zp = success["Facility Zip Code"].dropna().astype(str).str.strip()
    zp = zp[zp != ""]
    report["zip_codes"] = {
        "n_nonnull": len(zp),
        "valid_rate": rate(zp.str.match(ZIP_RE).sum(), len(zp)),
    }

    # Cross-field: capacity value should have a unit
    cap_val = success["Capacity Value"].notna() & (success["Capacity Value"].astype(str).str.strip() != "")
    cap_unit = success["Capacity Unit"].notna() & (success["Capacity Unit"].astype(str).str.strip() != "")
    report["capacity_consistency"] = {
        "rows_with_capacity_value": int(cap_val.sum()),
        "value_without_unit": int((cap_val & ~cap_unit).sum()),
        "value_with_unit_rate": rate(int((cap_val & cap_unit).sum()), int(cap_val.sum())),
    }

    # Sentinel leakage: 'ERROR' strings in content fields of successful rows
    content_cols = [c for c in df.columns if c not in ("Status", "Filename", "Processing Date", "Model Used")]
    err_counts = {}
    for col in content_cols:
        cnt = int((success[col].astype(str).str.strip().str.upper() == "ERROR").sum())
        if cnt:
            err_counts[col] = cnt
    report["error_sentinel_in_success_rows"] = err_counts

    # Duplicates: identical Filename + Unit ID pairs
    unit_rows = success[success["Unit ID"].notna() & (success["Unit ID"].astype(str).str.strip() != "")]
    dup_mask = unit_rows.duplicated(subset=["Filename", "Unit ID"], keep=False)
    report["duplicates"] = {
        "unit_rows": len(unit_rows),
        "rows_in_duplicate_filename_unitid_groups": int(dup_mask.sum()),
        "duplicate_rate": rate(int(dup_mask.sum()), len(unit_rows)),
    }

    # Missingness by field (successful rows)
    missing = {}
    for col in df.columns:
        vals = success[col]
        nonnull = vals.notna() & (vals.astype(str).str.strip() != "")
        missing[col] = rate(int((~nonnull).sum()), ns)
    report["missingness_success_rows"] = missing

    # Unit ID plausibility: empty or 1-char IDs among unit rows
    uid = unit_rows["Unit ID"].astype(str).str.strip()
    report["unit_id_quality"] = {
        "n": len(uid),
        "single_char_rate": rate(int((uid.str.len() <= 1).sum()), len(uid)),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "automated_qc_report.json").write_text(json.dumps(report, indent=2))

    lines = [
        "# Automated QC Report",
        "",
        f"Input: `{input_path.name}` — {n:,} rows ({ns:,} successful).",
        "",
        f"- Status distribution: {report['status_distribution']}",
        f"- ISO 8601 rate — Issuance Date: {dates['Issuance Date']['iso8601_rate']}, Expiration Date: {dates['Expiration Date']['iso8601_rate']}",
        f"- Valid state codes: {report['state_codes']['valid_rate']} (invalid examples: {list(report['state_codes']['invalid_values'])[:8]})",
        f"- Valid ZIP codes: {report['zip_codes']['valid_rate']}",
        f"- Capacity value accompanied by unit: {report['capacity_consistency']['value_with_unit_rate']}",
        f"- Duplicate Filename+Unit ID rate: {report['duplicates']['duplicate_rate']}",
        f"- 'ERROR' sentinel leakage into successful rows: {err_counts or 'none'}",
        "",
        "## Missingness (successful rows)",
        "",
        "| Field | Missing rate |",
        "|---|---|",
    ]
    for col, r in sorted(missing.items(), key=lambda kv: -(kv[1] or 0)):
        lines.append(f"| {col} | {r} |")
    (OUT_DIR / "automated_qc_report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "missingness_success_rows"}, indent=2))
    print("\nTop missing fields:", dict(sorted(missing.items(), key=lambda kv: -(kv[1] or 0))[:8]))


if __name__ == "__main__":
    main()
