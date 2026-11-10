#!/usr/bin/env python3
"""Manufacturing-only ECHO coverage benchmark for the data descriptor.

The all-sector benchmark (benchmark_coverage_vs_echo.py) matches dataset
facilities to every active major (Title V) air facility in EPA ECHO. The
manuscript presents the dataset as a resource on installed equipment at U.S.
manufacturing facilities, so its coverage figures are restricted to the ECHO
majors whose first AIRNAICS code is in sector 31-33. This script re-cuts the
existing match table rather than re-running the matcher, so the manufacturing
numbers are exactly consistent with the all-sector ones.

Inputs (all produced by benchmark_coverage_vs_echo.py):
    data/raw/echo_air_majors/echo_air_majors.csv       ECHO universe
    data/processed/analysis/echo_union_matches.csv     matched (state, ECHO name) pairs
    data/processed/analysis/asm_representativeness_state.csv   ASM state weights
    data/processed/analysis/cbp_representativeness_county.csv  CBP county counts

Outputs (--out-dir, default data/processed/analysis/mfg_benchmark/), with the
same column names as the all-sector tables so the figure builders can read
either directory:
    coverage_vs_echo_majors.csv        state table (manufacturing majors only)
    coverage_by_naics3.csv             3-digit subsector coverage + representation
    coverage_by_county.csv             county coverage (manufacturing majors)
    cbp_representativeness_county.csv  county coverage merged with CBP counts
    asm_representativeness_state.csv   state coverage merged with ASM weights
    summary.json                       the headline numbers quoted in the paper

Usage:
    python scripts/manuscript_mfg_benchmark.py [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "data/processed/analysis"
ECHO = ROOT / "data/raw/echo_air_majors/echo_air_majors.csv"

NAICS3_NAMES = {
    "311": "Food", "312": "Beverage & tobacco", "313": "Textile mills",
    "314": "Textile product mills", "315": "Apparel", "316": "Leather",
    "321": "Wood products", "322": "Paper", "323": "Printing",
    "324": "Petroleum & coal products", "325": "Chemicals",
    "326": "Plastics & rubber", "327": "Nonmetallic minerals",
    "331": "Primary metals", "332": "Fabricated metals", "333": "Machinery",
    "334": "Computer & electronics", "335": "Electrical equipment",
    "336": "Transportation equipment", "337": "Furniture",
    "339": "Miscellaneous manufacturing",
}


def tier(cov: float) -> str:
    if cov >= 0.7:
        return "well-covered"
    if cov >= 0.3:
        return "partial"
    return "sparse"


def coverage_table(df: pd.DataFrame, by: str) -> pd.DataFrame:
    g = df.groupby(by).agg(echo_active_major_facilities=("matched", "size"),
                           matched_facilities=("matched", "sum")).reset_index()
    g["matched_facilities"] = g["matched_facilities"].astype(int)
    g["coverage"] = (g["matched_facilities"] / g["echo_active_major_facilities"]).round(3)
    return g


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", type=Path, default=ANALYSIS / "mfg_benchmark")
    args = ap.parse_args()
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    echo = pd.read_csv(ECHO, dtype=str)
    matches = pd.read_csv(ANALYSIS / "echo_union_matches.csv", dtype=str)
    key = lambda s, n: s.str.strip().str.upper() + "|" + n.str.strip().str.upper()
    echo["key"] = key(echo["AIRState"], echo["AIRName"])
    matched_keys = set(key(matches["state"], matches["echo_name"]))
    echo["matched"] = echo["key"].isin(matched_keys)
    echo["naics2"] = echo["AIRNAICS"].str.extract(r"^\s*(\d{2})", expand=False)
    echo["naics3"] = echo["AIRNAICS"].str.extract(r"^\s*(\d{3})", expand=False)
    echo["mfg"] = echo["naics2"].isin(["31", "32", "33"])

    all_cov = echo["matched"].mean()
    mfg = echo[echo["mfg"]].copy()
    summary = {
        "echo_all_majors": int(len(echo)),
        "echo_all_matched": int(echo["matched"].sum()),
        "echo_all_coverage": round(all_cov, 4),
        "echo_mfg_majors": int(len(mfg)),
        "echo_mfg_matched": int(mfg["matched"].sum()),
        "echo_mfg_coverage": round(mfg["matched"].mean(), 4),
        "echo_mfg_share_of_universe": round(len(mfg) / len(echo), 4),
    }

    # --- by state ---------------------------------------------------------
    st = coverage_table(mfg, "AIRState").rename(columns={"AIRState": "state"})
    st["tier"] = st["coverage"].map(tier)
    st = st.sort_values("coverage", ascending=False)
    st.to_csv(out / "coverage_vs_echo_majors.csv", index=False)
    tiers = st.groupby("tier").agg(jurisdictions=("state", "size"),
                                   majors=("echo_active_major_facilities", "sum"),
                                   matched=("matched_facilities", "sum"))
    summary["tiers"] = {t: {k: int(v) for k, v in row.items()} for t, row in tiers.iterrows()}
    summary["zero_coverage"] = st[st["matched_facilities"] == 0][
        ["state", "echo_active_major_facilities"]].to_dict("records")
    summary["states_with_mfg_majors"] = int(len(st))
    summary["plain_mean_state_coverage"] = round(st["coverage"].mean(), 4)

    # --- by 3-digit subsector --------------------------------------------
    sub = coverage_table(mfg.dropna(subset=["naics3"]), "naics3")
    sub["sector"] = sub["naics3"].map(NAICS3_NAMES).fillna("Other")
    sub["share_of_echo_universe"] = (sub["echo_active_major_facilities"]
                                     / sub["echo_active_major_facilities"].sum()).round(4)
    sub["share_of_matched"] = (sub["matched_facilities"]
                               / sub["matched_facilities"].sum()).round(4)
    sub["representation_ratio"] = (sub["share_of_matched"]
                                   / sub["share_of_echo_universe"]).round(2)
    sub = sub.sort_values("share_of_echo_universe", ascending=False)
    sub.to_csv(out / "coverage_by_naics3.csv", index=False)
    big = sub[sub["share_of_echo_universe"] >= 0.05]
    summary["subsectors_ge5pct"] = big[["naics3", "sector", "share_of_echo_universe",
                                        "coverage", "representation_ratio"]].to_dict("records")
    summary["subsector_ratio_range_ge5pct"] = [float(big["representation_ratio"].min()),
                                               float(big["representation_ratio"].max())]

    # --- by county + CBP ----------------------------------------------------
    mfg["fips"] = mfg["FacFIPSCode"].fillna("").str.zfill(5)
    cty = coverage_table(mfg[mfg["fips"].str.len() == 5], "fips")
    cty["state"] = cty["fips"].map(mfg.drop_duplicates("fips").set_index("fips")["AIRState"])
    cty.to_csv(out / "coverage_by_county.csv", index=False)
    cbp_src = pd.read_csv(ANALYSIS / "cbp_representativeness_county.csv", dtype={"fips": str})
    cbp = cty.merge(cbp_src[["fips", "cbp_total_est", "cbp_mfg_est"]].drop_duplicates("fips"),
                    on="fips", how="left")
    cbp.to_csv(out / "cbp_representativeness_county.csv", index=False)
    has = cbp.dropna(subset=["cbp_mfg_est"]).copy()
    has["q"] = pd.qcut(has["cbp_mfg_est"].rank(method="first"), 4,
                       labels=["Q1", "Q2", "Q3", "Q4"])
    q = has.groupby("q", observed=True).agg(counties=("fips", "size"),
                                            echo=("echo_active_major_facilities", "sum"),
                                            matched=("matched_facilities", "sum"))
    q["coverage"] = (q["matched"] / q["echo"]).round(3)
    summary["cbp_quartiles"] = {str(k): {c: (float(v) if c == "coverage" else int(v))
                                         for c, v in row.items()} for k, row in q.iterrows()}
    summary["cbp_spearman_all"] = round(float(
        has["coverage"].corr(has["cbp_mfg_est"], method="spearman")), 3)
    sub3 = has[has["echo_active_major_facilities"] >= 3]
    summary["cbp_spearman_ge3"] = round(float(
        sub3["coverage"].corr(sub3["cbp_mfg_est"], method="spearman")), 3)
    summary["cbp_counties"] = int(len(has))

    # --- ASM state weighting ---------------------------------------------
    asm_src = pd.read_csv(ANALYSIS / "asm_representativeness_state.csv")
    asm_cols = [c for c in asm_src.columns if c.startswith("asm_")]
    asm = st.merge(asm_src[["state"] + asm_cols], on="state", how="left")
    asm.to_csv(out / "asm_representativeness_state.csv", index=False)
    hasa = asm.dropna(subset=["asm_valadd"]).copy()
    tot_va = hasa["asm_valadd"].sum()
    summary["asm_states"] = int(len(hasa))
    summary["asm_valadd_weighted_coverage"] = round(float(
        (hasa["coverage"] * hasa["asm_valadd"]).sum() / tot_va), 4)
    summary["asm_emp_weighted_coverage"] = round(float(
        (hasa["coverage"] * hasa["asm_emp"]).sum() / hasa["asm_emp"].sum()), 4)
    summary["asm_plain_mean_coverage"] = round(float(hasa["coverage"].mean()), 4)
    summary["asm_spearman_valadd"] = round(float(
        hasa["coverage"].corr(hasa["asm_valadd"], method="spearman")), 3)
    summary["asm_spearman_emp"] = round(float(
        hasa["coverage"].corr(hasa["asm_emp"], method="spearman")), 3)
    hasa["va_share"] = hasa["asm_valadd"] / tot_va
    gaps = hasa[hasa["coverage"] < 0.5].nlargest(10, "asm_valadd")
    summary["largest_undercovered_states"] = [
        {"state": r.state, "coverage": float(r.coverage),
         "va_share": round(float(r.va_share), 4),
         "majors": int(r.echo_active_major_facilities)} for r in gaps.itertuples()]
    for s in ["TX", "LA", "OH", "WI", "CA", "IN", "NJ", "OR", "KS", "AZ", "UT", "OK", "NE",
              "DE", "DC", "PR"]:
        r = asm[asm.state == s]
        if not r.empty:
            summary.setdefault("states", {})[s] = {
                "majors": int(r.echo_active_major_facilities.iloc[0]),
                "matched": int(r.matched_facilities.iloc[0]),
                "coverage": float(r.coverage.iloc[0]),
                "va_share": (round(float(r.asm_valadd.iloc[0] / tot_va), 4)
                             if pd.notna(r.asm_valadd.iloc[0]) else None)}

    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
