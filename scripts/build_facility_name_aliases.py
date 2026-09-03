"""Map union documents to the facility name their SOURCE AGENCY uses.

The ECHO coverage benchmark joins on the facility name the LLM read off the
permit, which is whatever string the document happens to print ("HOUSTON
REFINING LP - PLANT 1" vs ECHO's "LYONDELL HOUSTON REFINERY"). Every scrape
lane, though, carries the agency's own registry name for the document it
downloaded -- TCEQ's xRegEntName, MN's facility_name, NY's facility_name, and
so on. Those names are the ones ECHO's AIRName is derived from, so joining on
them recovers facilities the dataset already holds but that name matching
misses. On TX alone this is worth ~12 points of measured coverage.

This writes one alias row per (union document, source name). Feed it to
benchmark_coverage_vs_echo.py --aliases; it is an alternate NAME for a
document we already extracted, never a claim to a document we do not have.

Lane keys differ, so each lane declares how its index rows reach a union
Filename:
  path        basename stem of a local-path column == union filename stem
  docid_re    a regex on the union filename yields the index's document id
  docid_last  last >=5-digit token of the union filename is the document id
  token1      second '_'-delimited token of the union filename is a facility id

TCEQ leaves xRegEntName blank on ~90% of document rows, so a lane may declare
`group_col`: when the matched row has no name of its own, the names carried by
sibling rows sharing that column (for TCEQ, the permit id xPrimaryID) are used.

Usage:
    PYTHONPATH=. python scripts/build_facility_name_aliases.py \
        [--union data/processed/permit_data_union_v5c.csv] \
        [--out data/processed/analysis/facility_name_aliases.csv]
"""
import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

RAW = Path("data/raw")

LANES = [
    # --- TCEQ: the union filename embeds the CFR document id --------------
    dict(lane="tx_tceq_titlev", state="TX", kind="docid_re",
         index=RAW / "tx_tceq_titlev_permits/tx_cfr_full_index.csv",
         regex=r"^permit-[^_]+_(\d{4,})_", id_col="dDocName",
         name_cols=["xRegEntName"], group_col="xPrimaryID"),
    dict(lane="tx_tceq_dc_nsr", state="TX", kind="docid_last",
         index=RAW / "tx_tceq_dc_nsr_permits/tx_tceq_dc_nsr_index.csv",
         prefix="tx_", id_col="dDocName", name_cols=["xRegEntName"],
         group_col="xPrimaryID"),
    # --- lanes whose index records the local file it wrote ----------------
    dict(lane="il_epa_boa", state="IL", kind="path",
         index=RAW / "il_epa_boa_archive/il_epa_boa_archive_index.csv",
         path_col="local_path", name_cols=["facility_name"]),
    dict(lane="mn_pca", state="MN", kind="path",
         index=RAW / "mn_pca_air_latest_permits/mn_pca_air_latest_permits_index.csv",
         path_col="local_path", name_cols=["facility_name"]),
    dict(lane="ky_dep", state="KY", kind="path",
         index=RAW / "ky_dep_titlev_permits/ky_download_log.csv",
         path_col="local_name", name_cols=["AgencyName"]),
    dict(lane="mt_deq", state="MT", kind="path",
         index=RAW / "mt_deq_air_permits/mt_deq_air_permits_index.csv",
         path_col="local_path", name_cols=["permittee"]),
    dict(lane="mo_dnr", state="MO", kind="path",
         index=RAW / "mo_dnr_operating_permits/mo_dnr_operating_permits_index.csv",
         path_col="local_path", name_cols=["facility"]),
    dict(lane="ny_dec", state="NY", kind="path",
         index=RAW / "ny_issued_title_v/ny_issued_title_v_index.csv",
         path_col="permit_file", name_cols=["facility_name"]),
    dict(lane="ar_adeq", state="AR", kind="path",
         index=RAW / "ar_adeq_title_v_active/ar_adeq_title_v_active_index.csv",
         path_col="local_path", name_cols=["facility_name"]),
    dict(lane="sd_nsite", state="SD", kind="path",
         index=RAW / "sd_nsite_air_permits_to_operate/sd_nsite_air_permits_to_operate_index.csv",
         path_col="local_path", name_cols=["site_name"]),
    dict(lane="ut_daq", state="UT", kind="path",
         index=RAW / "ut_daq_title_v_issued/ut_daq_title_v_issued_index.csv",
         path_col="local_path", name_cols=["site_name"]),
    dict(lane="id_deq", state="ID", kind="path",
         index=RAW / "id_deq_air_tier1_permits/id_deq_air_tier1_permits_index.csv",
         path_col="local_path", name_cols=["permittee"]),
    # --- data-centre lanes: job key is "<lane>_<facility id>_..." ---------
    dict(lane="ga_epd_dc", state="GA", kind="token1",
         index=RAW / "ga_epd_dc_permits/ga_epd_dc_permits_index.csv",
         prefix="ga_", id_col="airs_number", name_cols=["facility_name"]),
    dict(lane="or_deq_dc", state="OR", kind="token1",
         index=RAW / "or_deq_dc_permits/or_deq_dc_permits_index.csv",
         prefix="or_", id_col="source_number",
         name_cols=["source_name", "plant_site_name"]),
    dict(lane="nv_ndep_dc", state="NV", kind="token1",
         index=RAW / "nv_ndep_dc_permits/nv_ndep_dc_permits_index.csv",
         prefix="nv_", id_col="facility_id",
         name_cols=["company_name", "facility_name"]),
    dict(lane="az_maricopa_dc", state="AZ", kind="token1",
         index=RAW / "az_maricopa_dc_permits/az_maricopa_dc_permits_index.csv",
         prefix="az_", id_col="facility_id", name_cols=["facility_nm"]),
]

DOCID_LAST_RE = re.compile(r"(?<!\d)(\d{5,})(?!\d)")
JUNK_NAMES = {"", "N/A", "NA", "UNKNOWN", "ERROR", "NONE"}


def read_index(path):
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def names_of(row, cols):
    out = []
    for c in cols:
        v = (row.get(c) or "").strip()
        if v and v.upper() not in JUNK_NAMES:
            out.append(v)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--union", type=Path,
                    default=Path("data/processed/permit_data_union_v5c.csv"))
    ap.add_argument("--out", type=Path,
                    default=Path("data/processed/analysis/facility_name_aliases.csv"))
    args = ap.parse_args()

    # union filename -> state (first non-empty state wins)
    union_state = {}
    with args.union.open(newline="", encoding="utf-8", errors="replace") as f:
        r = csv.reader(f)
        cols = next(r)
        fn_i, st_i = cols.index("Filename"), cols.index("Facility State Abbreviation")
        for row in r:
            if len(row) <= max(fn_i, st_i):
                continue
            fn = row[fn_i]
            if fn and not union_state.get(fn):
                union_state[fn] = row[st_i].strip().upper()
    print(f"union: {len(union_state):,} documents", flush=True)

    stem_to_file = defaultdict(list)
    for fn in union_state:
        stem_to_file[os.path.splitext(fn)[0].lower()].append(fn)

    aliases = defaultdict(set)   # filename -> {(state, name, lane)}
    for spec in LANES:
        idx = read_index(spec["index"])
        if idx is None:
            print(f"  {spec['lane']:16} SKIPPED (no index at {spec['index']})")
            continue
        hits = Counter()
        by_group = defaultdict(set)
        if spec.get("group_col"):
            for row in idx:
                g = (row.get(spec["group_col"]) or "").strip()
                if g:
                    by_group[g].update(names_of(row, spec["name_cols"]))

        def row_names(row):
            """The row's own name, else the names its permit-id siblings carry."""
            nms = names_of(row, spec["name_cols"])
            if not nms and spec.get("group_col"):
                g = (row.get(spec["group_col"]) or "").strip()
                nms = sorted(by_group.get(g, ()))
            return nms

        if spec["kind"] == "path":
            by_stem = defaultdict(list)
            for row in idx:
                for part in re.split(r"[;,|]", row.get(spec["path_col"]) or ""):
                    stem = os.path.splitext(os.path.basename(part.strip()))[0].lower()
                    if stem:
                        by_stem[stem].append(row)
            for stem, files in stem_to_file.items():
                for row in by_stem.get(stem, []):
                    for nm in row_names(row):
                        for fn in files:
                            aliases[fn].add((spec["state"], nm, spec["lane"]))
                            hits[fn] = 1
        else:
            by_id = defaultdict(list)
            for row in idx:
                key = (row.get(spec["id_col"]) or "").strip()
                if key:
                    by_id[key].append(row)
            for fn in union_state:
                stem = os.path.splitext(fn)[0]
                key = None
                if spec["kind"] == "docid_re":
                    m = re.match(spec["regex"], stem)
                    key = m.group(1) if m else None
                elif spec["kind"] == "docid_last":
                    if stem.startswith(spec["prefix"]):
                        ids = DOCID_LAST_RE.findall(stem)
                        key = ids[-1] if ids else None
                elif spec["kind"] == "token1":
                    if stem.startswith(spec["prefix"]):
                        parts = stem.split("_")
                        key = parts[1] if len(parts) > 1 else None
                if not key:
                    continue
                for row in by_id.get(key, []):
                    for nm in row_names(row):
                        aliases[fn].add((spec["state"], nm, spec["lane"]))
                        hits[fn] = 1
        n_names = len({(nm) for fn in hits for _, nm, ln in aliases[fn] if ln == spec["lane"]})
        print(f"  {spec['lane']:16} {spec['state']}  index rows {len(idx):>7,}  "
              f"union docs matched {len(hits):>6,}  distinct names {n_names:>5,}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with args.out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["filename", "state", "source_name", "lane"])
        for fn in sorted(aliases):
            for state, nm, lane in sorted(aliases[fn]):
                w.writerow([fn, state, nm, lane])
                n += 1
    print(f"\n{n:,} alias rows for {len(aliases):,} union documents -> {args.out}")
    disagree = sum(1 for fn in aliases
                   for st, _, _ in aliases[fn] if union_state.get(fn) and union_state[fn] != st)
    print(f"note: {disagree:,} alias rows whose lane state differs from the "
          f"extracted state (lane state wins)")


if __name__ == "__main__":
    main()
