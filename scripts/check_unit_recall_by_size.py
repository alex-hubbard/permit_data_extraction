"""Recall check: units extracted vs source document size across the union dataset.

Motivated by the ORNL INPLT cross-check (2026-07-06): two Frito-Lay permits
(433KB and 845KB of text) had their main boilers plainly in the text but
extraction returned only the small units. Chunking only triggers on an explicit
token-limit/truncation error, so a long document can "succeed" with silently
incomplete units.

Outputs:
  data/processed/analysis/unit_recall_by_size.csv        per-file table
  data/processed/analysis/unit_recall_suspects.csv       flagged large docs
and prints a size-binned summary.

Usage:
    PYTHONPATH=. python scripts/check_unit_recall_by_size.py
"""

import re
from pathlib import Path

import pandas as pd

UNION_CSV = Path("data/processed/permit_data_union.csv")
TEXT_DIR = Path("data/interim/extracted_text")
OUT_DIR = Path("data/processed/analysis")

# Equipment-ish keywords counted in source text for large docs (recall signal).
EQUIP_RE = re.compile(
    r"\b(boiler|furnace|heater|oven|dryer|kiln|incinerator|turbine|engine|"
    r"generator|melter|calciner|cracker|reformer)s?\b",
    re.IGNORECASE,
)

# Docs at or above this text size get the keyword scan.
SCAN_MIN_CHARS = 150_000


def main():
    df = pd.read_csv(UNION_CSV, low_memory=False)

    ok = df["Status"].astype(str).str.startswith("Success")
    has_unit = ok & (
        (df["Unit ID"].notna() & (df["Unit ID"].astype(str) != "ERROR"))
        | (df["Unit Description"].notna() & (df["Unit Description"].astype(str) != "ERROR"))
    )
    per_file = (
        df.assign(_unit=has_unit, _ok=ok)
        .groupby("Filename")
        .agg(
            n_rows=("Filename", "size"),
            n_units=("_unit", "sum"),
            extraction_ok=("_ok", "any"),
            source_run=("Source Run", "first"),
            model=("Model Used", "first"),
            state=("Facility State Abbreviation", "first"),
        )
        .reset_index()
    )
    n_failed = (~per_file["extraction_ok"]).sum()
    print(f"Files whose extraction FAILED outright (excluded from recall stats): {n_failed:,}")
    per_file = per_file[per_file["extraction_ok"]]

    sizes = {}
    for fn in per_file["Filename"]:
        p = TEXT_DIR / f"{fn}.txt"
        sizes[fn] = p.stat().st_size if p.exists() else None
    per_file["text_chars"] = per_file["Filename"].map(sizes)
    matched = per_file.dropna(subset=["text_chars"]).copy()
    matched["text_chars"] = matched["text_chars"].astype(int)
    print(
        f"Files in union: {len(per_file):,} | with text file: {len(matched):,} "
        f"({len(matched)/len(per_file):.0%})"
    )

    bins = [0, 25_000, 50_000, 100_000, 150_000, 250_000, 400_000, 700_000, 10**9]
    labels = ["<25K", "25-50K", "50-100K", "100-150K", "150-250K", "250-400K", "400-700K", ">700K"]
    matched["size_bin"] = pd.cut(matched["text_chars"], bins=bins, labels=labels)
    summary = matched.groupby("size_bin", observed=True).agg(
        files=("Filename", "size"),
        median_units=("n_units", "median"),
        mean_units=("n_units", "mean"),
        p90_units=("n_units", lambda s: s.quantile(0.9)),
        pct_le3_units=("n_units", lambda s: (s <= 3).mean() * 100),
    )
    # Units per 100K chars — the recall signal: should be roughly flat if
    # extraction keeps up with document length, falls off if it doesn't.
    matched["units_per_100k"] = matched["n_units"] / (matched["text_chars"] / 100_000)
    summary["median_units_per_100k"] = matched.groupby("size_bin", observed=True)[
        "units_per_100k"
    ].median()
    print("\n=== Units extracted by document size ===")
    print(summary.round(2).to_string())

    # Keyword scan on large docs only.
    large = matched[matched["text_chars"] >= SCAN_MIN_CHARS].copy()
    print(f"\nScanning {len(large):,} docs >= {SCAN_MIN_CHARS//1000}K chars for equipment keywords...")
    kw_counts = {}
    for fn in large["Filename"]:
        try:
            text = (TEXT_DIR / f"{fn}.txt").read_text(errors="ignore")
            kw_counts[fn] = len(EQUIP_RE.findall(text))
        except OSError:
            kw_counts[fn] = None
    large["equip_mentions"] = large["Filename"].map(kw_counts)
    large["mentions_per_unit"] = large["equip_mentions"] / large["n_units"].clip(lower=1)

    # Suspects: big doc, heavy equipment vocabulary, few extracted units.
    suspects = large[
        (large["equip_mentions"] >= 40) & (large["n_units"] <= 5)
    ].sort_values("mentions_per_unit", ascending=False)
    print(f"\n=== Suspected recall misses (>=40 equipment mentions, <=5 units extracted): {len(suspects)} ===")
    print(
        suspects[
            ["Filename", "state", "text_chars", "n_units", "equip_mentions", "mentions_per_unit", "source_run"]
        ]
        .head(40)
        .to_string(index=False)
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    matched.to_csv(OUT_DIR / "unit_recall_by_size.csv", index=False)
    large.sort_values("mentions_per_unit", ascending=False).to_csv(
        OUT_DIR / "unit_recall_suspects.csv", index=False
    )
    print(f"\nWrote {OUT_DIR/'unit_recall_by_size.csv'} and {OUT_DIR/'unit_recall_suspects.csv'}")


if __name__ == "__main__":
    main()
