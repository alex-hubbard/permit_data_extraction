"""Benchmark dataset coverage against EPA ECHO's universe of active major air
facilities, and test representativeness against Census County Business Patterns.

Pipeline:
  1. Download the facility-level list of active major (Title V universe) air
     facilities from the ECHO air REST API for every state (cached under
     data/raw/echo_air_majors/).
  2. Match union-dataset facilities to ECHO facilities within each state by
     normalized facility name (exact, then fuzzy via rapidfuzz). Coverage =
     fraction of ECHO majors matched by at least one union facility.
  3. Aggregate coverage nationally, by state (with well/partial/sparse tiers),
     by 2-digit NAICS sector, and by county FIPS.
  4. Merge county coverage with CBP county-level establishment counts (total
     and manufacturing) and report coverage by manufacturing-intensity
     quartile plus rank correlations — i.e., whether acquisition gaps are
     correlated with industrial activity.

Outputs (data/processed/analysis/):
  coverage_vs_echo_majors.csv        state table (superset of the old columns)
  coverage_by_naics.csv              sector coverage + matched-vs-universe shares
  coverage_by_county.csv             county FIPS coverage
  cbp_representativeness_county.csv  county coverage merged with CBP
  echo_union_matches.csv             matched pairs audit (name, score, type)

Caveats: name matching is conservative (normalized exact or token-set >= the
--threshold cutoff within the same state) but not perfect — chains with many
same-state plants can over-match, heavy name variants under-match. Treat
coverage as an estimate; the audit CSV supports spot-checking. The legacy
distinct-facility-name ratio is retained for continuity and can exceed 1.

Usage:
    python scripts/benchmark_coverage_vs_echo.py [--refresh-echo]
        [--threshold 93] [--cbp-year 2022]
"""

import argparse
import io
import json
import os
import re
import time
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

try:  # optional: read CENSUS_API_KEY from a .env like the rest of the project
    from dotenv import dotenv_values
except ImportError:
    dotenv_values = lambda *a, **k: {}

UNION_CSV = Path("data/processed/permit_data_union_v2.csv")
ANALYSIS_DIR = Path("data/processed/analysis")
ECHO_CACHE = Path("data/raw/echo_air_majors/echo_air_majors.csv")
CBP_CACHE_DIR = Path("data/raw/cbp")
ASM_CACHE_DIR = Path("data/raw/asm")

STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY PR"
).split()

ECHO_SEARCH = ("https://echodata.epa.gov/echo/air_rest_services.get_facilities"
               "?output=JSON&p_act=Y&p_maj=Y&responseset=1&p_st={st}")
# 1 AIRName, 2 SourceID, 5 AIRState, 9 AIRCounty, 14 FacFIPSCode,
# 22 AIRNAICS, 27 AIRStatus, 29 AIRClassification, 103 AIRMajorFlag
ECHO_DOWNLOAD = ("https://echodata.epa.gov/echo/air_rest_services.get_download"
                 "?output=CSV&qid={qid}&qcolumns=1,2,5,9,14,22,27,29,103")
CBP_URL = "https://www2.census.gov/programs-surveys/cbp/datasets/{y}/cbp{yy}co.zip"
# ASM Geographic Area Statistics time series (state x NAICS manufacturing).
# Requires a free Census API key (https://api.census.gov/data/key_signup.html);
# unlike CBP there is no clean flat file, so the key is mandatory for ASM.
ASM_URL = ("https://api.census.gov/data/timeseries/asm/area2017"
           "?get=GEO_ID,NAME,NAICS2017,NAICS2017_LABEL,VALADD,RCPTOT,EMP,PAYANN,CEXTOT,ESTAB"
           "&for=state:*&NAICS2017={naics}&time={year}&key={key}")

# FIPS state code -> USPS abbreviation (ASM reports numeric state FIPS)
STATE_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "72": "PR",
}

NAICS2_NAMES = {
    "11": "Agriculture/Forestry", "21": "Mining/Oil & Gas", "22": "Utilities",
    "23": "Construction", "31": "Manufacturing", "32": "Manufacturing",
    "33": "Manufacturing", "42": "Wholesale Trade", "44": "Retail Trade",
    "45": "Retail Trade", "48": "Transportation", "49": "Transportation/Warehousing",
    "51": "Information", "52": "Finance", "53": "Real Estate", "54": "Professional Svcs",
    "55": "Management", "56": "Admin/Waste Mgmt", "61": "Education",
    "62": "Health Care", "71": "Arts/Entertainment", "72": "Accommodation/Food",
    "81": "Other Services", "92": "Public Administration",
}

NAME_SUFFIXES = {
    "LLC", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
    "LP", "LLP", "LTD", "PLC", "LLLP", "PC", "PA",
}


def norm_name(s):
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in NAME_SUFFIXES)


def fetch_json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def fetch_echo_state(st, retries=5):
    delay = 5
    for attempt in range(retries):
        try:
            res = fetch_json(ECHO_SEARCH.format(st=st))["Results"]
            n, qid = int(res["QueryRows"]), res["QueryID"]
            if not n:
                return pd.DataFrame()
            with urllib.request.urlopen(ECHO_DOWNLOAD.format(qid=qid),
                                        timeout=120) as r:
                df = pd.read_csv(io.BytesIO(r.read()), dtype=str,
                                 keep_default_na=False)
            if len(df) != n:
                print(f"{st}: WARNING download rows {len(df)} != count {n}")
            return df
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"{st}: retrying in {delay}s ({e})")
            time.sleep(delay)
            delay *= 2


def fetch_echo_majors(refresh=False):
    cached = None
    if ECHO_CACHE.exists() and not refresh:
        cached = pd.read_csv(ECHO_CACHE, dtype=str, keep_default_na=False)
        todo = sorted(set(STATES) - set(cached["AIRState"]))
        if not todo:
            print(f"ECHO universe: using cache {ECHO_CACHE}")
            return cached
        print(f"ECHO cache missing {len(todo)} states ({' '.join(todo)}); fetching")
    else:
        todo = STATES
    frames = [cached] if cached is not None else []
    for st in todo:
        try:
            df = fetch_echo_state(st)
            print(f"{st}: {len(df)} active majors")
            if len(df):
                frames.append(df)
        except Exception as e:
            print(f"{st}: ECHO fetch FAILED after retries ({e})")
        time.sleep(2)
    echo = pd.concat(frames, ignore_index=True)
    ECHO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    echo.to_csv(ECHO_CACHE, index=False)
    print(f"ECHO universe: {len(echo):,} facilities -> {ECHO_CACHE}")
    return echo


def fetch_cbp_county(year):
    yy = str(year)[-2:]
    path = CBP_CACHE_DIR / f"cbp{yy}co.zip"
    if not path.exists():
        CBP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Downloading CBP {year} county file...")
        urllib.request.urlretrieve(CBP_URL.format(y=year, yy=yy), path)
    with zipfile.ZipFile(path) as z:
        cbp = pd.read_csv(z.open(z.namelist()[0]), dtype=str,
                          usecols=["fipstate", "fipscty", "naics", "est"])
    cbp["fips"] = cbp["fipstate"].str.zfill(2) + cbp["fipscty"].str.zfill(3)
    cbp["est"] = pd.to_numeric(cbp["est"], errors="coerce").fillna(0)
    total = cbp[cbp["naics"] == "------"].set_index("fips")["est"]
    mfg = (cbp[cbp["naics"].isin(["31----", "32----", "33----"])]
           .groupby("fips")["est"].sum())
    out = pd.DataFrame({"cbp_total_est": total, "cbp_mfg_est": mfg}).fillna(0)
    return out.reset_index().rename(columns={"index": "fips"})


def census_api_key():
    """Census API key from env or a project .env (CENSUS_API_KEY). ASM has no
    keyless flat file, so this is required for the ASM block; returns None to
    skip it gracefully."""
    return os.getenv("CENSUS_API_KEY") or dotenv_values().get("CENSUS_API_KEY")


def fetch_asm_state(year, key, naics="31-33"):
    """State-level ASM manufacturing statistics (value added, shipments,
    employment, payroll, capex, establishments). Cached as CSV so a rerun
    needs no key. ASM covers only manufacturing (NAICS 31-33), so this is a
    state economic-magnitude measure, complementing CBP's county establishment
    counts. Returns a DataFrame keyed by USPS state, or None if unavailable."""
    tag = naics.replace("-", "")
    path = ASM_CACHE_DIR / f"asm_state_{year}_{tag}.csv"
    if not path.exists():
        if not key:
            return None
        ASM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        url = ASM_URL.format(naics=naics, year=year, key=key)
        print(f"Downloading ASM {year} state file (NAICS {naics})...")
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                rows = json.loads(r.read())
        except Exception as e:  # network / bad key / no data for year
            print(f"  ASM fetch failed ({e}); skipping ASM block.")
            return None
        df = pd.DataFrame(rows[1:], columns=rows[0])
        df.to_csv(path, index=False)
    else:
        df = pd.read_csv(path, dtype=str)
    num = ["VALADD", "RCPTOT", "EMP", "PAYANN", "CEXTOT", "ESTAB"]
    for c in num:
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    df["state"] = df["state"].str.zfill(2).map(STATE_FIPS)
    df = df.dropna(subset=["state"])
    return df.rename(columns={c: f"asm_{c.lower()}" for c in num})[
        ["state"] + [f"asm_{c.lower()}" for c in num]]


def match_union_to_echo(echo, union_fac, threshold):
    """Flag each ECHO facility matched/unmatched by a union facility name
    (same state, normalized-exact or fuzzy token_set >= threshold)."""
    echo = echo.copy()
    echo["norm"] = echo["AIRName"].map(norm_name)
    by_state = {st: g for st, g in
                union_fac.groupby("state")["norm"].agg(lambda s: sorted(set(s))).items()}
    matched, match_rows = [], []
    for st, g in echo.groupby("AIRState"):
        names = by_state.get(st, [])
        name_set = set(names)
        for idx, en in zip(g.index, g["norm"]):
            if en in name_set:
                matched.append(idx)
                match_rows.append((st, echo.at[idx, "AIRName"], en, 100, "exact"))
            elif len(en) >= 6 and names:
                hit = process.extractOne(en, names, scorer=fuzz.token_set_ratio,
                                         score_cutoff=threshold)
                if hit:
                    matched.append(idx)
                    match_rows.append((st, echo.at[idx, "AIRName"], hit[0],
                                       round(hit[1], 1), "fuzzy"))
    echo["matched"] = echo.index.isin(set(matched))
    audit = pd.DataFrame(match_rows, columns=["state", "echo_name",
                                              "union_norm_name", "score", "type"])
    return echo, audit


def coverage_table(echo, by, extra_cols=None):
    g = echo.groupby(by, dropna=False)
    t = pd.DataFrame({
        "echo_active_major_facilities": g.size(),
        "matched_facilities": g["matched"].sum().astype(int),
    })
    t["coverage"] = (t["matched_facilities"]
                     / t["echo_active_major_facilities"]).round(3)
    if extra_cols:
        t = t.join(extra_cols)
    return t.reset_index()


def tier(cov):
    if pd.isna(cov):
        return ""
    return "well-covered" if cov >= 0.7 else ("partial" if cov >= 0.3 else "sparse")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--union", help="union CSV to benchmark (default: the module's UNION_CSV)")
    ap.add_argument("--refresh-echo", action="store_true")
    ap.add_argument("--threshold", type=float, default=93,
                    help="rapidfuzz token_set_ratio cutoff for fuzzy matches")
    ap.add_argument("--cbp-year", type=int, default=2022)
    ap.add_argument("--asm-year", type=int, default=2021,
                    help="ASM vintage (2021 is the latest non-Economic-Census year)")
    args = ap.parse_args()
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    global UNION_CSV
    if args.union:
        UNION_CSV = Path(args.union)
    print(f"union: {UNION_CSV}")

    u = pd.read_csv(UNION_CSV, dtype=str, keep_default_na=False,
                    usecols=["Filename", "Facility State Abbreviation",
                             "Facility Name"])
    files = u.drop_duplicates("Filename")
    docs = files["Facility State Abbreviation"].value_counts()
    union_fac = (u[u["Facility Name"] != ""]
                 .assign(norm=lambda d: d["Facility Name"].map(norm_name),
                         state=lambda d: d["Facility State Abbreviation"])
                 .query("norm != '' and state in @STATES")
                 .drop_duplicates(["state", "norm"]))
    fac_counts = union_fac.groupby("state").size()

    echo = fetch_echo_majors(refresh=args.refresh_echo)
    echo, audit = match_union_to_echo(echo, union_fac, args.threshold)
    audit.to_csv(ANALYSIS_DIR / "echo_union_matches.csv", index=False)

    n_echo, n_matched = len(echo), int(echo["matched"].sum())
    print(f"\n=== National coverage ===")
    print(f"ECHO active majors: {n_echo:,} | matched by union: {n_matched:,} "
          f"({n_matched / n_echo:.1%})")
    print(f"Union distinct facility names (in-state): {len(union_fac):,} "
          f"(unmatched union names include minor sources and name variants)")

    # --- by state (superset of the legacy table) ---
    st = coverage_table(echo, "AIRState").rename(columns={"AIRState": "state"})
    st["union_permit_docs"] = st["state"].map(docs).fillna(0).astype(int)
    st["union_distinct_facility_names"] = st["state"].map(fac_counts).fillna(0).astype(int)
    st["legacy_name_count_ratio"] = (st["union_distinct_facility_names"]
                                     / st["echo_active_major_facilities"]).round(3)
    st["tier"] = st["coverage"].map(tier)
    st = st.sort_values("coverage", ascending=False)
    st.to_csv(ANALYSIS_DIR / "coverage_vs_echo_majors.csv", index=False)
    print(f"\n=== Coverage by state ===")
    print(st.to_string(index=False))
    tiers = st.groupby("tier")["echo_active_major_facilities"].agg(["count", "sum"])
    print(f"\nState tiers (>=70% well-covered, 30-70% partial, <30% sparse):")
    print(tiers.rename(columns={"count": "states", "sum": "echo_majors"}).to_string())

    # --- by NAICS sector ---
    echo["naics2"] = echo["AIRNAICS"].str.extract(r"^\s*(\d{2})", expand=False)
    nx = coverage_table(echo, "naics2").rename(columns={"naics2": "naics2"})
    nx["sector"] = nx["naics2"].map(NAICS2_NAMES).fillna("Unknown/blank")
    nx["share_of_echo_universe"] = (nx["echo_active_major_facilities"] / n_echo).round(4)
    nx["share_of_matched"] = (nx["matched_facilities"] / n_matched).round(4)
    nx["representation_ratio"] = (nx["share_of_matched"]
                                  / nx["share_of_echo_universe"]).round(2)
    nx = nx.sort_values("echo_active_major_facilities", ascending=False)
    nx.to_csv(ANALYSIS_DIR / "coverage_by_naics.csv", index=False)
    print(f"\n=== Coverage by NAICS sector (top 12 by universe size) ===")
    print(nx.head(12).to_string(index=False))

    # --- by county + CBP representativeness ---
    echo["fips"] = echo["FacFIPSCode"].str.zfill(5)
    cty = coverage_table(echo[echo["fips"].str.len() == 5], "fips")
    cty["state"] = cty["fips"].map(
        echo.drop_duplicates("fips").set_index("fips")["AIRState"])
    cty.to_csv(ANALYSIS_DIR / "coverage_by_county.csv", index=False)

    cbp = fetch_cbp_county(args.cbp_year)
    rep = cty.merge(cbp, on="fips", how="left")
    rep.to_csv(ANALYSIS_DIR / "cbp_representativeness_county.csv", index=False)

    has_cbp = rep.dropna(subset=["cbp_mfg_est"])
    has_cbp = has_cbp[has_cbp["echo_active_major_facilities"] > 0].copy()
    has_cbp["mfg_quartile"] = pd.qcut(has_cbp["cbp_mfg_est"].rank(method="first"),
                                      4, labels=["Q1 (least mfg)", "Q2", "Q3",
                                                 "Q4 (most mfg)"])
    q = has_cbp.groupby("mfg_quartile", observed=True).agg(
        counties=("fips", "size"),
        echo_majors=("echo_active_major_facilities", "sum"),
        matched=("matched_facilities", "sum"))
    q["coverage"] = (q["matched"] / q["echo_majors"]).round(3)
    print(f"\n=== CBP {args.cbp_year} representativeness "
          f"(counties with >=1 ECHO major, by mfg-establishment quartile) ===")
    print(q.to_string())
    for label, sub in [("all counties", has_cbp),
                       (">=3 ECHO majors", has_cbp[
                           has_cbp["echo_active_major_facilities"] >= 3])]:
        rho = sub["coverage"].corr(sub["cbp_mfg_est"], method="spearman")
        print(f"Spearman(county coverage, mfg establishments), {label} "
              f"(n={len(sub)}): {rho:.3f}")
    gaps = has_cbp[(has_cbp["echo_active_major_facilities"] >= 5)
                   & (has_cbp["matched_facilities"] == 0)]
    print(f"Counties with >=5 ECHO majors and zero matches: {len(gaps)} "
          f"(top: {gaps.nlargest(5, 'echo_active_major_facilities')[['fips', 'state', 'echo_active_major_facilities']].to_dict('records')})")

    # --- state economic-weight representativeness (ASM) ---
    asm = fetch_asm_state(args.asm_year, census_api_key())
    if asm is None:
        print("\n=== ASM representativeness: SKIPPED ===")
        print("  Set CENSUS_API_KEY (free: https://api.census.gov/data/key_signup.html) "
              "to add state manufacturing value-added / employment weighting.")
    else:
        srep = st.merge(asm, on="state", how="left")
        srep.to_csv(ANALYSIS_DIR / "asm_representativeness_state.csv", index=False)
        has = srep.dropna(subset=["asm_valadd"]).copy()
        # coverage of the ECHO universe weighted by each state's manufacturing
        # value added: what fraction of US manufacturing output sits in states
        # where we cover the air majors well?
        tot_va = has["asm_valadd"].sum()
        va_wtd_cov = (has["coverage"] * has["asm_valadd"]).sum() / tot_va
        emp_wtd_cov = (has["coverage"] * has["asm_emp"]).sum() / has["asm_emp"].sum()
        unwtd = has["coverage"].mean()
        print(f"\n=== ASM {args.asm_year} state economic-weight representativeness "
              f"(manufacturing NAICS 31-33, n={len(has)} states) ===")
        print(f"  Plain mean state coverage:              {unwtd:.1%}")
        print(f"  Value-added-weighted state coverage:    {va_wtd_cov:.1%}")
        print(f"  Employment-weighted state coverage:     {emp_wtd_cov:.1%}")
        print("  (weighted > plain => we cover economically larger mfg states better)")
        for metric in ["asm_valadd", "asm_emp"]:
            rho = has["coverage"].corr(has[metric], method="spearman")
            print(f"  Spearman(state coverage, {metric[4:]:7s}): {rho:+.3f}")
        # biggest economic blind spots: high manufacturing value added, low coverage
        has["va_share"] = (has["asm_valadd"] / tot_va).round(4)
        gaps = has[has["coverage"] < 0.5].nlargest(10, "asm_valadd")
        print(f"  Largest under-covered mfg states (coverage <50%, by value added):")
        for _, r in gaps.iterrows():
            print(f"    {r['state']}: {r['coverage']:.0%} covered, "
                  f"{r['va_share']:.1%} of US mfg value added "
                  f"(${r['asm_valadd']/1e6:.1f}B)")

    outs = ("coverage_vs_echo_majors.csv, coverage_by_naics.csv, "
            "coverage_by_county.csv, cbp_representativeness_county.csv, "
            "echo_union_matches.csv"
            + (", asm_representativeness_state.csv" if asm is not None else ""))
    print(f"\n-> {ANALYSIS_DIR}/{outs}")


if __name__ == "__main__":
    main()
