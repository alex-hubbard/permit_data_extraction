"""Combine the resolved-NAICS table with the LLM stage into naics_final.csv.

backfill_naics.py resolves what it can from the permit, ECHO, the pipeline's
derived code, and the SIC crosswalk; classify_naics_llm.py answers the
remainder. This is the join the release column is read from
(add_resolved_naics_to_union.py, build_dashboard_ready_parquet.py):

    resolved code wins; where there is none, an LLM answer that came back as a
    real 6-digit code fills in with naics_source = "llm"; anything still
    unanswered stays blank in both columns.

Usage:
    PYTHONPATH=. python scripts/build_naics_final.py
"""
import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

csv.field_size_limit(sys.maxsize)

ANALYSIS = Path("data/processed/analysis")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--resolved", type=Path, default=ANALYSIS / "naics_resolved.csv")
    ap.add_argument("--llm", type=Path, default=ANALYSIS / "naics_llm_results.csv")
    ap.add_argument("--out", type=Path, default=ANALYSIS / "naics_final.csv")
    args = ap.parse_args()

    llm = {}
    if args.llm.exists():
        for r in csv.DictReader(args.llm.open(encoding="utf-8", errors="replace")):
            code = (r.get("naics_llm") or "").strip()
            if len(code) == 6 and code.isdigit():
                llm[r["filename"]] = code
    print(f"LLM answers with a code: {len(llm):,}")

    src = Counter()
    n = 0
    with args.resolved.open(encoding="utf-8", errors="replace", newline="") as fin, \
            args.out.open("w", encoding="utf-8", newline="") as fout:
        w = csv.DictWriter(fout, fieldnames=["filename", "naics_final", "naics_source"])
        w.writeheader()
        for r in csv.DictReader(fin):
            code, source = r["naics_resolved"], r["naics_source"]
            if not code:
                code = llm.get(r["filename"], "")
                source = "llm" if code else ""
            w.writerow({"filename": r["filename"], "naics_final": code,
                        "naics_source": source})
            src[source or "unclassified"] += 1
            n += 1

    classified = n - src["unclassified"]
    print(f"{args.out}: {n:,} permits, {classified:,} classified "
          f"({classified / n:.1%})")
    for s, c in src.most_common():
        print(f"  {s:<14} {c:>7,} ({c / n:.1%})")


if __name__ == "__main__":
    main()
