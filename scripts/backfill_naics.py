"""Resolve a usable NAICS code per permit, with provenance.

Industry classification in the union is sparse and partly wrong (permit-stated
NAICS on ~22% of permits; SIC codes sitting in the NAICS column; semicolon-
joined multi-code values; a derived `Classified NAICS` that agrees with a
permit-stated code only 43% of the time and over-assigns 339999). This builds
one resolved code per permit from the best available source, in priority
order, and records which source won so downstream users can filter on it:

  permit   a valid 6-digit NAICS stated in the permit itself
  echo     EPA ECHO's AIRNAICS for the facility this permit matched
  derived  the pipeline's Classified NAICS, excluding the 339999 catch-all
  sic      crosswalked from a permit-stated SIC code (2-digit sector only)
  (blank)  none of the above -> candidate for LLM classification

Stage 1 (this script) writes the resolved table and a work-list of permits
still unclassified. Stage 2 classifies that list with an LLM.

Usage:
    PYTHONPATH=. python scripts/backfill_naics.py \
        [--union data/processed/permit_data_union_v5a.csv]
Outputs (data/processed/analysis/):
    naics_resolved.csv        filename, state, facility, resolved code, source
    naics_llm_worklist.csv    permits still unclassified, with the text an LLM needs
"""
import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
csv.field_size_limit(sys.maxsize)

ECHO = Path("data/raw/echo_air_majors/echo_air_majors.csv")
MATCHES = Path("data/processed/analysis/echo_union_matches.csv")
OUT_DIR = Path("data/processed/analysis")

NAME_SUFFIXES = {"INC", "LLC", "LP", "LTD", "CO", "CORP", "CORPORATION", "COMPANY",
                 "THE", "PLANT", "FACILITY"}
VALID_SECTORS = {"11", "21", "22", "23", "31", "32", "33", "42", "44", "45", "48",
                 "49", "51", "52", "53", "54", "55", "56", "61", "62", "71", "72",
                 "81", "92"}
JUNK_CODES = {"339999"}  # "All Other Miscellaneous Manufacturing" catch-all

# SIC division -> NAICS sector, for the coarse fallback only (2-digit).
SIC_TO_NAICS_SECTOR = {
    **{f"{i:02d}": "11" for i in range(1, 10)},
    **{f"{i:02d}": "21" for i in range(10, 15)},
    **{f"{i:02d}": "23" for i in range(15, 18)},
    **{f"{i:02d}": "31" for i in (20, 21, 22, 23)},
    **{f"{i:02d}": "32" for i in (24, 25, 26, 27, 28, 29, 30, 31, 32)},
    **{f"{i:02d}": "33" for i in (33, 34, 35, 36, 37, 38, 39)},
    **{f"{i:02d}": "48" for i in range(40, 48)},
    "49": "22",
    **{f"{i:02d}": "42" for i in range(50, 52)},
    **{f"{i:02d}": "44" for i in range(52, 60)},
}


def norm_name(s):
    s = (s or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in NAME_SUFFIXES)


def first_valid_naics(value):
    """First 6-digit token with a real NAICS sector, from a possibly multi-code field."""
    for tok in re.split(r"[;,/|\s]+", (value or "").strip()):
        d = re.sub(r"\D", "", tok)
        if len(d) == 6 and d[:2] in VALID_SECTORS:
            return d
    return None


def sic_sector(value):
    for tok in re.split(r"[;,/|\s]+", (value or "").strip()):
        d = re.sub(r"\D", "", tok)
        if len(d) == 4:
            return SIC_TO_NAICS_SECTOR.get(d[:2])
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--union", type=Path,
                    default=Path("data/processed/permit_data_union_v5a.csv"))
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    echo_naics = {}
    for r in csv.DictReader(ECHO.open(encoding="utf-8", errors="replace")):
        code = first_valid_naics(r.get("AIRNAICS"))
        if code:
            echo_naics[(r["AIRState"], norm_name(r["AIRName"]))] = code
    print(f"ECHO facilities with a usable NAICS: {len(echo_naics):,}")

    # matched union facility name -> ECHO code, via the benchmark's match table
    matched = {}
    if MATCHES.exists():
        for r in csv.DictReader(MATCHES.open(encoding="utf-8", errors="replace")):
            code = echo_naics.get((r["state"], norm_name(r["echo_name"])))
            if code:
                matched[(r["state"], r["union_norm_name"])] = code
    print(f"union facility names carrying an ECHO NAICS: {len(matched):,}")

    permits = {}
    with args.union.open(encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            if r["Status"] != "Success" or r["Filename"] in permits:
                continue
            permits[r["Filename"]] = r
    print(f"successful permits: {len(permits):,}")

    src_counts = Counter()
    rows, worklist = [], []
    for fn, r in permits.items():
        state = (r.get("Facility State Abbreviation") or "").strip().upper()
        fac = (r.get("Facility Name") or "").strip()
        code = first_valid_naics(r.get("NAICS Code"))
        source = "permit" if code else None
        if not code:
            code = matched.get((state, norm_name(fac)))
            source = "echo" if code else None
        if not code:
            c = first_valid_naics(r.get("Classified NAICS"))
            if c and c not in JUNK_CODES:
                code, source = c, "derived"
        if not code:
            sec = sic_sector(r.get("SIC Code"))
            if sec:
                code, source = sec, "sic"
        src_counts[source or "unresolved"] += 1
        rows.append({"filename": fn, "state": state, "facility": fac,
                     "naics_resolved": code or "", "naics_source": source or ""})
        if not code:
            worklist.append({
                "filename": fn, "state": state, "facility": fac,
                "industry_description": (r.get("Industry Description") or "").strip(),
                "unit_description": (r.get("Unit Description") or "").strip()[:200],
                "permit_type": (r.get("Permit Type") or "").strip(),
            })

    with (OUT_DIR / "naics_resolved.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "state", "facility",
                                          "naics_resolved", "naics_source"])
        w.writeheader()
        w.writerows(rows)
    with (OUT_DIR / "naics_llm_worklist.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "state", "facility",
                                          "industry_description", "unit_description",
                                          "permit_type"])
        w.writeheader()
        w.writerows(worklist)

    tot = len(rows)
    print("\nresolution by source:")
    for s, c in src_counts.most_common():
        print(f"  {s:<12} {c:>7,} ({c/tot:.1%})")
    resolved = tot - src_counts["unresolved"]
    print(f"\nresolved: {resolved:,}/{tot:,} ({resolved/tot:.1%})  "
          f"-> LLM work-list: {len(worklist):,}")
    with_name = sum(1 for w in worklist if w["facility"] or w["industry_description"])
    print(f"  of those, {with_name:,} have facility/industry text an LLM can use")


if __name__ == "__main__":
    main()
