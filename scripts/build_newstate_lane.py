"""Build the extraction lane for staged (scraped-but-never-extracted) corpora.

Maps each staged raw PDF directory to its extracted-text stems, drops anything
already present in the union, applies per-source dedupe (latest revision per
facility where the corpus is revision-stacked), and writes one lane CSV that
build_vertex_batch_inputs.py consumes.

Usage: python scripts/build_newstate_lane.py [--union data/processed/permit_data_union_v4.csv]
Output: data/processed/gcp_batch/newstate_lane.csv
"""
import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TEXT_DIR = Path("data/interim/extracted_text")
OUT = Path("data/processed/gcp_batch/newstate_lane.csv")

# source -> (pdf dir, state)
SOURCES = {
    "tx": ("data/raw/tx_tceq_titlev_permits/pdfs", "TX"),
    "wi": ("data/raw/wi_dnr_titlev_permits/pdfs", "WI"),
    "la": ("data/raw/la_edms_titlev_permits/pdfs", "LA"),
    "scaqmd": ("data/raw/aqmd_titlev_permits/pdfs", "CA"),
    "sjvapcd": ("data/raw/sjvapcd_titlev_permits/pdfs", "CA"),
    "baaqmd": ("data/raw/baaqmd_titlev_permits/pdfs", "CA"),
    "mn": ("data/raw/mn_pca_air_latest_permits", "MN"),
    "ky": ("data/raw/ky_dep_titlev_permits/pdfs", "KY"),
    "co_drive": ("data/raw/co_titlev_permits/pdfs", "CO"),
    "co_onbase": ("data/raw/co_onbase_titlev_permits/pdfs", "CO"),
    "ok": ("data/raw/ok_deq_titlev_permits/pdfs", "OK"),
    "ohio": ("data/raw/ohio_epa_air_permits", "OH"),
}

# Revision-stacked sources only: key = facility, keep the highest revision.
# SJVAPCD is deliberately NOT here — its filenames are
# <facility>_<facility>-<device>-<rev>, so per-facility dedupe would throw
# away distinct emission units; the portal already serves only current PTOs.
REV_PATTERNS = {
    "scaqmd": re.compile(r"^facID-(\d+)_rev-(\d+)"),
}

OHIO_FID_RX = re.compile(r"\b(\d{10})\b")
OHIO_DATE_RX = re.compile(r" - (\d{6,8}) - ")


def ohio_dedupe(stems):
    """Ohio's eDocs sweep pulls the full doc history (39,981 files / 9,265
    facilities, median 2 and up to 168 per facility, including drafts and
    non-permit correspondence). Keep the latest-dated APPROVAL doc per
    facility ID — 23.5K -> ~4.7K docs with no loss of current-permit coverage.
    """
    best = {}
    for s in stems:
        if "APPROVAL" not in s.upper():
            continue
        m = OHIO_FID_RX.search(s)
        if not m:
            continue
        d = OHIO_DATE_RX.search(s)
        date = d.group(1) if d else "0"
        key = (len(date), date)
        if m.group(1) not in best or key > best[m.group(1)][0]:
            best[m.group(1)] = (key, s)
    return {v[1] for v in best.values()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--union", default="data/processed/permit_data_union_v4.csv")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    in_union = set()
    with open(args.union, encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            in_union.add(r["Filename"])
    print(f"union filenames: {len(in_union):,}")

    rows = []
    for src, (pdf_dir, state) in SOURCES.items():
        d = Path(pdf_dir)
        if not d.exists():
            print(f"{src:<10} MISSING {pdf_dir}")
            continue
        stems = {p.stem for p in d.glob("*.pdf")}
        have_text = {s for s in stems if (TEXT_DIR / (s + ".txt")).exists()}
        fresh = have_text - in_union
        rx = REV_PATTERNS.get(src)
        if src == "ohio":
            kept = ohio_dedupe(fresh)
        elif rx:
            best = {}
            for s in fresh:
                m = rx.match(s)
                if not m:
                    best[s] = (0, s)
                    continue
                fac, rev = m.group(1), int(m.group(2))
                if fac not in best or rev > best[fac][0]:
                    best[fac] = (rev, s)
            kept = {v[1] for v in best.values()}
        else:
            kept = fresh
        for s in sorted(kept):
            size = (TEXT_DIR / (s + ".txt")).stat().st_size
            rows.append({"filename": s, "source": src, "state": state,
                         "text_bytes": size})
        print(f"{src:<10} {state}  pdfs {len(stems):>6}  text {len(have_text):>6}  "
              f"new {len(fresh):>6}  after-dedupe {len(kept):>6}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "source", "state", "text_bytes"])
        w.writeheader()
        w.writerows(rows)
    tok = sum(r["text_bytes"] for r in rows) / 4
    print(f"\n{args.out}: {len(rows):,} docs, ~{tok/1e6:.0f}M input tokens")


if __name__ == "__main__":
    main()
