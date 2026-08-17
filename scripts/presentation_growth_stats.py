"""Compute the v4 -> v5b growth statistics cited in the August 2026 briefing.

Reproduces every number on the deck's "How the Database Has Grown" and
"The August Run: Where Growth Came From" slides:

  - rows / documents / permits / facilities per release
  - equipment records added by state (top movers)
  - facilities added by state (top movers)
  - manufacturing share of the additions (records and facilities),
    using the resolved-NAICS table (permit > ECHO > derived > SIC > LLM)
    with the in-row NAICS/Classified NAICS as fallback

Facilities are (state, lowercased facility name) pairs, matching the
release-level counts quoted in the deck (33,067 for v5b). Note the
dashboard's KPI counts *sites* (name+city+state, 35,574), which runs
slightly higher.

Usage:
    python scripts/presentation_growth_stats.py \
        [--old data/processed/permit_data_union_v4.csv] \
        [--new data/processed/permit_data_union_v5b.csv]
"""
import argparse
import csv
from collections import Counter
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
NAICS_FINAL = REPO / "data/processed/analysis/naics_final.csv"
MFG_PREFIXES = tuple(str(x) for x in range(311, 340))
USECOLS = ["Filename", "Facility Name", "Facility State Abbreviation",
           "Permit Number", "NAICS Code", "Classified NAICS"]


def load_naics_final() -> dict:
    out = {}
    if NAICS_FINAL.exists():
        with NAICS_FINAL.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["naics_final"]:
                    out[r["filename"]] = r["naics_final"]
    else:
        print(f"NOTE: {NAICS_FINAL} missing; falling back to in-row codes only")
    return out


def scan(path: Path, naics_final: dict) -> dict:
    """One streaming pass: totals, per-state counts, and manufacturing flags."""
    s = {"rows": 0, "mfg_rows": 0, "docs": set(), "permits": set(),
         "facs": set(), "mfg_facs": set(),
         "state_rows": Counter(), "state_facs": {}}
    for ch in pd.read_csv(path, usecols=USECOLS, chunksize=500_000, dtype=str):
        code = ch["Filename"].map(naics_final)
        fallback = (ch["NAICS Code"].fillna(ch["Classified NAICS"])
                    .str.replace(r"\D", "", regex=True))
        code = code.fillna(fallback)
        mfg = code.str[:3].isin(MFG_PREFIXES).fillna(False)

        state = ch["Facility State Abbreviation"].str.strip().str.upper().fillna("?")
        named = ch["Facility Name"].notna()
        fac_all = state + "|" + ch["Facility Name"].str.strip().str.lower()

        s["rows"] += len(ch)
        s["mfg_rows"] += int(mfg.sum())
        s["docs"].update(ch["Filename"].dropna())
        s["permits"].update((state + "|" + ch["Permit Number"].fillna(""))
                            [ch["Permit Number"].notna()])
        s["facs"].update(fac_all[named])
        s["mfg_facs"].update(fac_all[named & mfg])
        for st_, cnt in state.value_counts().items():
            s["state_rows"][st_] += int(cnt)
        # keep state and facility row-aligned (a dropna() before this zip
        # silently shifts facilities into the wrong states)
        for st_, f in zip(state[named], fac_all[named]):
            s["state_facs"].setdefault(st_, set()).add(f)
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old", type=Path,
                    default=REPO / "data/processed/permit_data_union_v4.csv")
    ap.add_argument("--new", type=Path,
                    default=REPO / "data/processed/permit_data_union_v5b.csv")
    ap.add_argument("--top", type=int, default=8, help="states to list per table")
    args = ap.parse_args()

    naics_final = load_naics_final()
    old = scan(args.old, naics_final)
    new = scan(args.new, naics_final)

    def line(label, a, b):
        d = b - a
        pct = f" ({d / a * 100:+.0f}%)" if a else ""
        print(f"{label:28s} {a:>12,} -> {b:>12,}  {d:+,}{pct}")

    print(f"== {args.old.name} -> {args.new.name} ==")
    line("Rows", old["rows"], new["rows"])
    line("Documents", len(old["docs"]), len(new["docs"]))
    line("Permits (state|number)", len(old["permits"]), len(new["permits"]))
    line("Facilities (state|name)", len(old["facs"]), len(new["facs"]))

    added_rows = new["rows"] - old["rows"]
    added_mfg_rows = new["mfg_rows"] - old["mfg_rows"]
    new_facs = new["facs"] - old["facs"]
    new_mfg_facs = {f for f in new["mfg_facs"] if f not in old["facs"]}
    print(f"\nManufacturing share of additions (NAICS 31-33, resolved codes):")
    print(f"  records:    {added_mfg_rows:,} of {added_rows:,} "
          f"({added_mfg_rows / added_rows:.0%})")
    print(f"  facilities: {len(new_mfg_facs):,} of {len(new_facs):,} "
          f"({len(new_mfg_facs) / len(new_facs):.0%})")
    print(f"  whole release: {new['mfg_rows']:,} of {new['rows']:,} rows "
          f"({new['mfg_rows'] / new['rows']:.0%}) are manufacturing")

    print(f"\nEquipment records added by state (top {args.top}):")
    deltas = {st_: new["state_rows"][st_] - old["state_rows"].get(st_, 0)
              for st_ in new["state_rows"]}
    for st_, d in sorted(deltas.items(), key=lambda kv: -kv[1])[:args.top]:
        print(f"  {st_:4s} {d:+12,}")

    print(f"\nFacilities added by state (top {args.top}):")
    fac_deltas = {st_: len(new["state_facs"].get(st_, set()))
                  - len(old["state_facs"].get(st_, set()))
                  for st_ in new["state_facs"]}
    for st_, d in sorted(fac_deltas.items(), key=lambda kv: -kv[1])[:args.top]:
        print(f"  {st_:4s} {d:+12,}")


if __name__ == "__main__":
    main()
