"""Join the resolved NAICS code and its provenance onto the union dataset.

`backfill_naics.py` + `classify_naics_llm.py` resolve one industry code per
permit into `data/processed/analysis/naics_final.csv`, but that lives beside
the union rather than in it. The manuscript describes `NAICS Resolved` and
`NAICS Source` as release columns (Sections 3.2, 5.2, 5.5), so the deposited
file has to carry them.

This streams the union row by row -- the file is ~900 MB and pandas has OOM'd
on it before -- and writes a copy with the two columns inserted after
`Classified NAICS`. Permits absent from the resolution (those whose status is
not "Success", which the backfill skips) get empty values in both columns.

Usage:
    PYTHONPATH=. python scripts/add_resolved_naics_to_union.py \
        [--union data/processed/permit_data_union_v5a.csv] \
        [--out   data/processed/permit_data_union_v5b.csv]
"""
import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

csv.field_size_limit(sys.maxsize)

RESOLVED = Path("data/processed/analysis/naics_final.csv")
ANCHOR = "Classified NAICS"
NEW_COLS = ["NAICS Resolved", "NAICS Source"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--union", type=Path,
                    default=Path("data/processed/permit_data_union_v5a.csv"))
    ap.add_argument("--out", type=Path,
                    default=Path("data/processed/permit_data_union_v5b.csv"))
    ap.add_argument("--resolved", type=Path, default=RESOLVED)
    args = ap.parse_args()

    codes = {}
    with args.resolved.open(encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            codes[r["filename"]] = (r["naics_final"], r["naics_source"])
    print(f"resolved permits: {len(codes):,}")

    src_rows = Counter()
    permits_seen, permits_unmatched = set(), set()

    with args.union.open(encoding="utf-8", errors="replace", newline="") as fin, \
            args.out.open("w", encoding="utf-8", newline="") as fout:
        reader = csv.DictReader(fin)
        cols = list(reader.fieldnames)
        for c in NEW_COLS:
            if c in cols:
                raise SystemExit(f"{args.union} already has a {c!r} column")
        at = cols.index(ANCHOR) + 1
        cols[at:at] = NEW_COLS
        writer = csv.DictWriter(fout, fieldnames=cols)
        writer.writeheader()

        for row in reader:
            fn = row["Filename"]
            permits_seen.add(fn)
            code, source = codes.get(fn, ("", ""))
            if fn not in codes:
                permits_unmatched.add(fn)
            row["NAICS Resolved"] = code
            row["NAICS Source"] = source
            src_rows[source or "(none)"] += 1
            writer.writerow(row)

    total = sum(src_rows.values())
    print(f"rows written: {total:,}  columns: {len(cols)}")
    print(f"permits: {len(permits_seen):,}  without a resolution: "
          f"{len(permits_unmatched):,}")
    for k, v in src_rows.most_common():
        print(f"  {k}: {v:,} rows ({v / total * 100:.1f}%)")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
