"""Merge the free-model (lbl/gemma-4-thinking) re-extraction into the union -> v3.

Merge-not-replace, as mandated by the 2026-07-15 pilot QC (gemma misses
genuine in-text units in a small tail of files, so its rows may only ADD):

- Files with existing Success rows: keep ALL old union rows; append only
  free-model unit rows whose unit key (Unit ID, else description hash --
  mirrors dataset._unit_key) is not already present for that file. Old
  "Success (No Units Found)" placeholders are dropped once the file has
  real unit rows.
- Previously-failed files (LLM Extraction Failed / Text Reading Failed,
  incl. the recovered no-text docs): free-model rows replace the failure row.
- Appended rows are tagged Source Run = freemodel_2026-07; split-hours
  columns are renamed to the union's names; permit-level derived columns
  (Classified NAICS etc.) are carried over per-Filename; Pieces Failed kept.

Dry-run by default: prints aggregate stats and writes a per-file comparison
CSV (data/processed/analysis/freemodel_merge_dryrun.csv) without touching the
union. Pass --write to also write data/processed/permit_data_union_v3.csv.

Safe to rerun while the scale-up is still going -- it merges whatever the
finalized rows CSV contains. Refresh that first:

    PYTHONPATH=. python scripts/batch_reextract.py --finalize \
        --results-jsonl data/processed/reextraction/freemodel_results.jsonl

Usage:
    python scripts/merge_freemodel_into_union.py [--write]
"""

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

UNION_CSV = Path("data/processed/permit_data_union_v2.csv")
FM_ROWS_CSV = Path("data/processed/reextraction/freemodel_results_rows.csv")
FM_JSONL = Path("data/processed/reextraction/freemodel_results.jsonl")
OUT_CSV = Path("data/processed/permit_data_union_v3.csv")
REPORT_CSV = Path("data/processed/analysis/freemodel_merge_dryrun.csv")

SOURCE_RUN_TAG = "freemodel_2026-07"
PROCESSING_DATE = "2026-07-16"
NO_UNITS = "Success (No Units Found)"

RENAME = {
    "Operating Hours Value": "Operating Hours — numeric/value portion",
    "Operating Hours Time Basis": "Operating Hours — unit or basis (e.g. hours/year)",
    "Annual Run Hours Value": "Annual Run Hours — numeric/value portion",
    "Annual Run Hours Time Basis": "Annual Run Hours — unit or basis (e.g. hours/year)",
}
CARRY_OVER = ["Classified NAICS", "Latest Facility Filename", "Duplicate Equipment Documents"]

# unit-level fields used for the "which side is richer" diagnostic
UNIT_FIELDS = [
    "Unit Description", "Unit Quantity", "Unit Make", "Unit Model",
    "Year of Manufacture", "Unit Type", "Pollutants", "Emission Limits",
    "Opacity Limit", "Throughput/Production Limit", "Control Device(s)",
    "Capacity Value", "Capacity Unit", "Fuel Type", "Rated Efficiency",
    "Annual Run Hours", "Generation Capacity", "Applicable NESHAP/NSPS Subpart",
]


def unit_key(uid, desc):
    """Mirror dataset._unit_key: stripped Unit ID, else hash of description."""
    uid = (uid or "").strip()
    if uid:
        return uid
    return "desc_" + hashlib.md5((desc or "").encode("utf-8")).hexdigest()[:12]


def loose_key(key):
    """Aggressive normalization to spot near-duplicate IDs ('CP12 (Old)' vs 'CP12-Old')."""
    return "".join(ch for ch in key.lower() if ch.isalnum())


def doc_chars_map():
    out = {}
    if FM_JSONL.exists():
        for line in FM_JSONL.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "doc_chars" in rec:
                out[rec["filename"]] = rec["doc_chars"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="write permit_data_union_v3.csv (default: dry-run report only)")
    args = ap.parse_args()

    union = pd.read_csv(UNION_CSV, dtype=str, keep_default_na=False)
    fm = pd.read_csv(FM_ROWS_CSV, dtype=str, keep_default_na=False)
    fm = fm.rename(columns=RENAME)
    print(f"union v2: {len(union):,} rows / {union['Filename'].nunique():,} files")
    print(f"free-model rows: {len(fm):,} rows / {fm['Filename'].nunique():,} files")

    fm_files = set(fm["Filename"])
    in_scope = union["Filename"].isin(fm_files)
    passthrough = union[~in_scope]
    old = union[in_scope].copy()

    old_success_files = set(old.loc[old["Status"].str.startswith("Success"), "Filename"])
    failed_files = fm_files - old_success_files  # replace outright
    not_in_union = fm_files - set(old["Filename"])
    if not_in_union:
        print(f"WARNING: {len(not_in_union)} free-model files absent from the union entirely")

    # unit keys of existing real unit rows, per file
    old_units = old[old["Status"] == "Success"].copy()
    old_units["_key"] = [unit_key(u, d) for u, d in
                         zip(old_units["Unit ID"], old_units["Unit Description"])]
    old_keys = old_units.groupby("Filename")["_key"].agg(set).to_dict()

    fm_units = fm[fm["Status"] == "Success"].copy()
    fm_units["_key"] = [unit_key(u, d) for u, d in
                        zip(fm_units["Unit ID"], fm_units["Unit Description"])]
    fm_placeholder = fm[fm["Status"] == NO_UNITS]

    # which free-model unit rows get appended
    def is_new(row):
        return (row["Filename"] in failed_files
                or row["_key"] not in old_keys.get(row["Filename"], set()))

    fm_units["_new"] = fm_units.apply(is_new, axis=1)
    added = fm_units[fm_units["_new"]]
    files_gaining = set(added["Filename"])

    # old rows kept: everything except failure rows for replaced files and
    # no-units placeholders for files that now have real unit rows
    files_with_units = set(old_units["Filename"]) | files_gaining
    drop = (old["Filename"].isin(failed_files)
            | ((old["Status"] == NO_UNITS) & old["Filename"].isin(files_with_units)))
    old_kept = old[~drop]

    # free-model placeholders survive only for replaced files that got no units
    ph_keep = fm_placeholder[fm_placeholder["Filename"].isin(failed_files)
                             & ~fm_placeholder["Filename"].isin(files_gaining)]

    # ---- per-file dry-run report ----
    chars = doc_chars_map()
    fm_key_sets = fm_units.groupby("Filename")["_key"].agg(set).to_dict()
    fm_by_key = {(r["Filename"], r["_key"]): r for _, r in fm_units.iterrows()}
    old_by_key = {(r["Filename"], r["_key"]): r for _, r in old_units.iterrows()}

    def filled(row):
        return sum(1 for f in UNIT_FIELDS if str(row.get(f, "")).strip())

    report = []
    for fn in sorted(fm_files):
        o_keys = old_keys.get(fn, set())
        f_keys = fm_key_sets.get(fn, set())
        add_keys = f_keys - o_keys if fn not in failed_files else f_keys
        shared = f_keys & o_keys
        old_loose = {loose_key(k) for k in o_keys}
        near_dupes = sum(1 for k in add_keys if loose_key(k) in old_loose)
        fm_richer = sum(
            1 for k in shared
            if filled(fm_by_key[(fn, k)]) > filled(old_by_key[(fn, k)])
        )
        report.append({
            "Filename": fn,
            "Doc Chars": chars.get(fn, ""),
            "Old Status": "failed" if fn in failed_files else "success",
            "Old Units": len(o_keys),
            "FM Units": len(f_keys),
            "Added": len(add_keys),
            "Shared": len(shared),
            "Old Only (gemma missed)": len(o_keys - f_keys),
            "Near-Dupe Added": near_dupes,
            "FM Richer On Shared": fm_richer,
            "Pieces Failed": fm.loc[fm["Filename"] == fn, "Pieces Failed"].max(),
        })
    rep = pd.DataFrame(report)
    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    rep.to_csv(REPORT_CSV, index=False)

    # ---- aggregate stats ----
    n_recovered = len(failed_files & files_gaining)
    print(f"\nmerge scope: {len(fm_files):,} free-model files "
          f"({len(failed_files):,} previously failed, {n_recovered:,} of them now have units)")
    print(f"old real-unit rows kept: {len(old_kept[old_kept['Status'] == 'Success']):,}")
    print(f"free-model rows appended: {len(added):,} "
          f"(suppressed {len(fm_units) - len(added):,} already-present unit keys)")
    print(f"unit count: {rep['Old Units'].sum():,} -> {(rep['Old Units'] + rep['Added']).sum():,} "
          f"(+{rep['Added'].sum():,})")
    print(f"files where gemma missed old units (kept anyway): "
          f"{(rep['Old Only (gemma missed)'] > 0).sum():,} "
          f"({rep['Old Only (gemma missed)'].sum():,} units)")
    print(f"near-dupe added keys (ID formatting variants): {rep['Near-Dupe Added'].sum():,}")
    print(f"shared units where free-model row is richer: {rep['FM Richer On Shared'].sum():,}")
    print(f"per-file report -> {REPORT_CSV}")

    top = rep.nlargest(10, "Added")[["Filename", "Doc Chars", "Old Units", "Added"]]
    print("\ntop gains:")
    print(top.to_string(index=False))

    if not args.write:
        print("\ndry run — pass --write to produce", OUT_CSV)
        return

    new_rows = pd.concat([added.drop(columns=["_key", "_new"]), ph_keep], ignore_index=True)
    new_rows["Source Run"] = SOURCE_RUN_TAG
    new_rows["Processing Date"] = PROCESSING_DATE
    carry = old.groupby("Filename")[CARRY_OVER].first()
    for col in CARRY_OVER:
        new_rows[col] = new_rows["Filename"].map(carry[col])
    for col in union.columns:
        if col not in new_rows.columns:
            new_rows[col] = ""
    new_rows = new_rows[list(union.columns)]

    merged = pd.concat([passthrough, old_kept, new_rows], ignore_index=True)
    merged.to_csv(OUT_CSV, index=False)
    print(f"\n-> {OUT_CSV}: {len(merged):,} rows / {merged['Filename'].nunique():,} files")
    print(merged["Source Run"].value_counts().to_string())


if __name__ == "__main__":
    main()
