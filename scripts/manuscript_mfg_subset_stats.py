#!/usr/bin/env python3
"""Statistics for the manufacturing subset of the release, for the manuscript.

The release table holds every permit the pipeline processed. The data
descriptor presents the manufacturing component (permits whose resolved NAICS
code is in sector 31-33), so every headline number has to be computed twice:
for the whole table and for that subset. This script streams the release CSV
once and writes both.

The manufacturing membership of a document is read from the resolved-NAICS
table (naics_final.csv), not from the release's own ``NAICS Resolved`` column,
so the numbers can be refreshed after the LLM classification stage without
rewriting the release file.

Inputs:
    --union   release CSV (default data/processed/permit_data_union_v5f.csv)
    --naics   data/processed/analysis/naics_final.csv (filename, naics_final, naics_source)
Outputs (--out-dir, default data/processed/analysis/mfg_subset/):
    summary.json            headline counts for both scopes
    fill_rates.csv          share of successful rows with a value, per column, both scopes
    states.csv              documents and records per state, both scopes; unit rows and facilities (mfg)
    equipment_categories.csv  unit rows per Equipment Category, manufacturing scope
    naics3.csv              manufacturing documents and facilities by 3-digit subsector
    naics2_all.csv          all classified documents by 2-digit sector
    models.csv              records by extraction model, both scopes
    address_groups.csv      same-address document groups (for the facility dedup check)

Usage:
    python scripts/manuscript_mfg_subset_stats.py [--union CSV] [--naics CSV] [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.csv as pa_csv

ROOT = Path(__file__).resolve().parents[1]
csv.field_size_limit(sys.maxsize)

STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
             "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
             "WV WI WY PR VI GU AS MP".split())
ZIP_RX = re.compile(r"^\d{5}(-\d{4})?$")
ISO_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SUFFIX_RX = re.compile(r"\b(INC|LLC|LP|LTD|CO|CORP|CORPORATION|COMPANY|THE|PLANT|FACILITY)\b")
NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
SCOPES = ("all", "mfg")

MFG_SECTORS = {"31", "32", "33"}


def norm_name(s: str) -> str:
    s = NON_ALNUM.sub(" ", (s or "").upper())
    s = SUFFIX_RX.sub(" ", s)
    return " ".join(s.split())


def norm_addr(s: str) -> str:
    s = NON_ALNUM.sub(" ", (s or "").upper())
    s = re.sub(r"\b(STREET|ST|ROAD|RD|AVENUE|AVE|DRIVE|DR|HIGHWAY|HWY|BOULEVARD|BLVD|"
               r"LANE|LN|NORTH|N|SOUTH|S|EAST|E|WEST|W|SUITE|STE)\b", " ", s)
    return " ".join(s.split())


def zip5(s: str) -> str:
    m = re.match(r"\s*(\d{5})", s or "")
    return m.group(1) if m else ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--union", type=Path,
                    default=ROOT / "data/processed/permit_data_union_v5f.csv")
    ap.add_argument("--naics", type=Path,
                    default=ROOT / "data/processed/analysis/naics_final.csv")
    ap.add_argument("--out-dir", type=Path,
                    default=ROOT / "data/processed/analysis/mfg_subset")
    ap.add_argument("--block-mb", type=int, default=32)
    args = ap.parse_args()
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    naics = {}
    with args.naics.open(encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            if r["naics_final"]:
                # stripped like the release values below: 21 manufacturing documents
                # have a trailing space in their filename and were missed (2026-10-26)
                naics[r["filename"].strip()] = (r["naics_final"], r["naics_source"])
    mfg_docs = {fn for fn, (code, _) in naics.items() if code[:2] in MFG_SECTORS}
    print(f"resolved codes: {len(naics):,}; manufacturing documents: {len(mfg_docs):,}")

    with args.union.open(encoding="utf-8", newline="") as f:
        cols = next(csv.reader(f))
    col = {c: c for c in cols}
    # column aliases used below (the release uses the short names)
    C_STATUS, C_FN, C_MODEL, C_STATE = "Status", "Filename", "Model Used", "Facility State Abbreviation"
    C_NAME, C_ADDR, C_CITY, C_ZIP = "Facility Name", "Facility Address", "Facility City", "Facility Zip Code"
    C_UID, C_UDESC, C_CAPV, C_CAPU = "Unit ID", "Unit Description", "Capacity Value", "Capacity Unit"
    C_ISS, C_EXP, C_PN = "Issuance Date", "Expiration Date", "Permit Number"
    C_RUN, C_PF, C_LFF, C_DUP = "Source Run", "Pieces Failed", "Latest Facility Filename", "Duplicate Equipment Documents"
    C_EQC, C_EQS = "Equipment Category", "Equipment Subcategory"
    # the flag was Combustion Source, then Equipment Type (10-23), now Equipment Designation (10-26)
    C_COMB = next(c for c in ("Equipment Designation", "Equipment Type", "Combustion Source") if c in col)
    C_FUEL, C_GEN, C_EFF, C_ARH = "Fuel Type", "Generation Capacity", "Rated Efficiency", "Annual Run Hours"
    for c in (C_STATUS, C_FN, C_MODEL, C_STATE, C_UID, C_EQC, C_COMB, C_RUN, C_PF):
        assert c in col, f"missing column {c}"

    rows_by_status = {s: Counter() for s in SCOPES}
    fill = {s: Counter() for s in SCOPES}          # non-empty values among Success rows
    success_rows = {s: 0 for s in SCOPES}
    unit_rows = {s: 0 for s in SCOPES}             # Status == Success
    models = {s: Counter() for s in SCOPES}
    runs = {s: Counter() for s in SCOPES}
    state_rows = {s: Counter() for s in SCOPES}
    eq_cat = Counter(); eq_sub = Counter(); comb = Counter()
    eq_cat_cap = Counter()                          # unit rows with a capacity value, by category
    fuel = Counter()
    fmt = {s: Counter() for s in SCOPES}
    unit_key = {s: Counter() for s in SCOPES}
    unit_key3 = {s: Counter() for s in SCOPES}
    doc = {}                                        # filename -> first-row info
    pf_docs = set()                                 # documents with any non-zero Pieces Failed

    read_opts = pa_csv.ReadOptions(block_size=args.block_mb << 20)
    parse_opts = pa_csv.ParseOptions(newlines_in_values=True)
    conv_opts = pa_csv.ConvertOptions(column_types={c: pa.string() for c in cols},
                                      strings_can_be_null=False)
    reader = pa_csv.open_csv(args.union, read_options=read_opts,
                             parse_options=parse_opts, convert_options=conv_opts)
    n_batches = 0
    for batch in reader:
        n_batches += 1
        df = batch.to_pandas()
        for c in df.columns:
            df[c] = df[c].fillna("").str.strip()
        df["_mfg"] = df[C_FN].isin(mfg_docs)
        is_success = df[C_STATUS] == "Success"
        is_ok = is_success | (df[C_STATUS] == "Success (No Units Found)")
        for scope in SCOPES:
            m = df["_mfg"] if scope == "mfg" else pd.Series(True, index=df.index)
            sub = df[m]
            rows_by_status[scope].update(sub[C_STATUS].value_counts().to_dict())
            ok = sub[is_ok[m]]
            success_rows[scope] += len(ok)
            for c in cols:
                fill[scope][c] += int((ok[c] != "").sum())
            models[scope].update(ok[C_MODEL].value_counts().to_dict())
            runs[scope].update(ok[C_RUN].value_counts().to_dict())
            state_rows[scope].update(ok[C_STATE].value_counts().to_dict())
            st_ = ok[C_STATE][ok[C_STATE] != ""]
            fmt[scope]["state_present"] += len(st_)
            fmt[scope]["state_valid"] += int(st_.isin(STATES).sum())
            z = ok[C_ZIP][ok[C_ZIP] != ""]
            fmt[scope]["zip_present"] += len(z)
            fmt[scope]["zip_valid"] += int(z.str.match(ZIP_RX).sum())
            for c, k in ((C_ISS, "iss"), (C_EXP, "exp")):
                d = ok[c][ok[c] != ""]
                fmt[scope][f"{k}_present"] += len(d)
                fmt[scope][f"{k}_iso"] += int(d.str.match(ISO_RX).sum())
            cap = ok[ok[C_CAPV] != ""]
            fmt[scope]["cap_value"] += len(cap)
            fmt[scope]["cap_value_with_unit"] += int((cap[C_CAPU] != "").sum())
            units = sub[is_success[m]]
            unit_rows[scope] += len(units)
            keyed = units[units[C_UID] != ""]          # blank unit IDs are not duplicates
            unit_key[scope].update(zip(keyed[C_FN], keyed[C_UID]))
            unit_key3[scope].update(
                hash(t) for t in zip(keyed[C_FN], keyed[C_UID], keyed[C_UDESC]))
            fmt[scope]["unit_rows_with_uid"] += len(keyed)
            for c in (C_GEN, C_EFF, C_ARH):
                fmt[scope][f"units_missing_{c}"] += int((units[c] == "").sum())
            if scope == "mfg":
                eq_cat.update(units[C_EQC].value_counts().to_dict())
                eq_sub.update(units[C_EQS].value_counts().to_dict())
                comb.update(units[C_COMB].value_counts().to_dict())
                eq_cat_cap.update(units.loc[units[C_CAPV] != "", C_EQC].value_counts().to_dict())
                fuel.update(units.loc[units[C_FUEL] != "", C_FUEL].str.lower().value_counts().to_dict())
        pf = pd.to_numeric(df[C_PF], errors="coerce").fillna(0)
        pf_docs.update(df.loc[pf > 0, C_FN])
        # first-row document info
        first = df.drop_duplicates(C_FN)
        for d in first.to_dict("records"):
            fn = d[C_FN]
            if fn in doc:
                continue
            doc[fn] = {
                "fn": fn, "status": d[C_STATUS], "state": d[C_STATE], "model": d[C_MODEL],
                "run": d[C_RUN], "pf": d[C_PF], "name": d[C_NAME], "addr": d[C_ADDR],
                "city": d[C_CITY], "zip": zip5(d[C_ZIP]), "pn": d[C_PN],
                "lff": d[C_LFF], "dup": d[C_DUP], "rows": 0,
            }
        for fn, n in df[C_FN].value_counts().items():
            doc[fn]["rows"] += int(n)
        if n_batches % 5 == 0:
            print(f"  batch {n_batches}: {len(doc):,} documents so far", flush=True)

    # Status per document: a document's rows share one status except for the
    # rare mixed cases; use the first row.
    for fn, d in doc.items():
        d["mfg"] = fn in mfg_docs
        d["naics"], d["source"] = naics.get(fn, ("", ""))
        d["fac_key"] = norm_name(d["name"]) + "|" + d["state"] + "|" + (d["zip"] or d["city"].upper())

    docs_all = list(doc.values())
    docs_mfg = [d for d in docs_all if d["mfg"]]
    summary = {"scopes": {}}
    for scope, ds in (("all", docs_all), ("mfg", docs_mfg)):
        s = {
            "documents": len(ds),
            "documents_by_status": dict(Counter(d["status"] for d in ds)),
            "records": sum(rows_by_status[scope].values()),
            "records_by_status": dict(rows_by_status[scope]),
            "successful_records": success_rows[scope],
            "unit_records": unit_rows[scope],
            "documents_pieces_failed": sum(1 for d in ds if d["fn"] in pf_docs),
            "states_with_documents": len({d["state"] for d in ds if d["state"] in STATES}),
            "documents_blank_state": sum(1 for d in ds if d["state"] == ""),
            "documents_invalid_state": sum(1 for d in ds if d["state"] and d["state"] not in STATES),
            "distinct_facility_keys": len({d["fac_key"] for d in ds if d["name"]}),
            "distinct_latest_facility_filename": len({d["lff"] for d in ds if d["lff"]}),
            "models_records": dict(models[scope].most_common()),
            "runs_records": dict(runs[scope].most_common()),
            "format": dict(fmt[scope]),
            "dup_unit_keys": {
                "unit_rows": unit_rows[scope],
                "rows_sharing_fn_uid": sum(n for n in unit_key[scope].values() if n > 1),
                "groups_fn_uid": sum(1 for n in unit_key[scope].values() if n > 1),
                "rows_sharing_fn_uid_desc": sum(n for n in unit_key3[scope].values() if n > 1),
            },
            "documents_dc_lanes": sum(1 for d in ds if d["run"].startswith(("dc_generators", "gcp_dclane"))),
        }
        summary["scopes"][scope] = s
    succ_docs = [d for d in docs_all if d["status"] == "Success"]
    summary["classification"] = {
        "documents_with_success_row": len(succ_docs),
        "documents_resolved": sum(1 for d in succ_docs if d["naics"]),
        "documents_unresolved": sum(1 for d in succ_docs if not d["naics"]),
        "documents_no_success_row": len(docs_all) - len(succ_docs),
        "resolved_by_source": dict(Counter(d["source"] for d in succ_docs if d["naics"])),
        "mfg_documents": len(docs_mfg),
        "mfg_by_source": dict(Counter(d["source"] for d in docs_mfg)),
        "mfg_share_of_resolved": round(len(docs_mfg) / max(1, sum(1 for d in succ_docs if d["naics"])), 4),
    }
    # sectors
    n2 = Counter(d["naics"][:2] for d in succ_docs if d["naics"])
    pd.DataFrame(sorted(n2.items()), columns=["naics2", "documents"]).to_csv(out / "naics2_all.csv", index=False)
    n3 = defaultdict(lambda: {"documents": 0, "facilities": set()})
    for d in docs_mfg:
        n3[d["naics"][:3]]["documents"] += 1
        n3[d["naics"][:3]]["facilities"].add(d["fac_key"])
    pd.DataFrame([{"naics3": k, "documents": v["documents"], "facilities": len(v["facilities"])}
                  for k, v in sorted(n3.items())]).to_csv(out / "naics3.csv", index=False)
    # states
    st_rows = []
    for st_ in sorted({d["state"] for d in docs_all if d["state"]}):
        st_rows.append({"state": st_,
                        "documents_all": sum(1 for d in docs_all if d["state"] == st_),
                        "records_all": state_rows["all"].get(st_, 0),
                        "documents_mfg": sum(1 for d in docs_mfg if d["state"] == st_),
                        "records_mfg": state_rows["mfg"].get(st_, 0),
                        "unit_rows_mfg": sum(d["rows"] for d in docs_mfg if d["state"] == st_ and d["status"] == "Success"),
                        "facilities_mfg": len({d["fac_key"] for d in docs_mfg if d["state"] == st_ and d["name"]})})
    pd.DataFrame(st_rows).sort_values("records_mfg", ascending=False).to_csv(out / "states.csv", index=False)
    # fill rates
    pd.DataFrame([{"column": c,
                   "fill_all": round(fill["all"][c] / max(1, success_rows["all"]), 4),
                   "fill_mfg": round(fill["mfg"][c] / max(1, success_rows["mfg"]), 4)}
                  for c in cols]).to_csv(out / "fill_rates.csv", index=False)
    # equipment
    eq = pd.DataFrame([{"Equipment Category": k, "unit_rows": v,
                        "share": round(v / max(1, unit_rows["mfg"]), 4),
                        "rows_with_capacity": eq_cat_cap.get(k, 0)}
                       for k, v in eq_cat.most_common()])
    eq.to_csv(out / "equipment_categories.csv", index=False)
    pd.DataFrame([{"Equipment Subcategory": k, "unit_rows": v} for k, v in eq_sub.most_common()]
                 ).to_csv(out / "equipment_subcategories.csv", index=False)
    summary["mfg_equipment"] = {"categories": dict(eq_cat.most_common()),
                                "combustion": dict(comb.most_common()),
                                "top_fuels": dict(fuel.most_common(12))}
    pd.DataFrame([{"model": k, "records_all": models["all"].get(k, 0), "records_mfg": v}
                  for k, v in models["all"].most_common()]).to_csv(out / "models.csv", index=False)
    # same-address groups (facility-level dedup check)
    groups = defaultdict(list)
    for d in docs_all:
        if d["addr"] and d["state"]:
            groups[(d["state"], norm_addr(d["addr"]), d["zip"])].append(d)
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    rows = []
    for k, v in multi.items():
        names = {norm_name(d["name"]) for d in v}
        pns = {d["pn"] for d in v if d["pn"]}
        rows.append({"state": k[0], "address": k[1], "zip": k[2], "documents": len(v),
                     "distinct_names": len(names), "distinct_permit_numbers": len(pns),
                     "names": " || ".join(sorted(names))[:300],
                     "mfg_documents": sum(1 for d in v if d["mfg"])})
    ag = pd.DataFrame(rows).sort_values(["distinct_names", "documents"], ascending=False)
    ag.to_csv(out / "address_groups.csv", index=False)
    summary["address_groups"] = {
        "documents_with_address": sum(len(v) for v in groups.values()),
        "addresses": len(groups),
        "addresses_with_multiple_documents": len(multi),
        "addresses_with_multiple_names": int((ag["distinct_names"] > 1).sum()) if len(ag) else 0,
        "addresses_with_multiple_permit_numbers": int((ag["distinct_permit_numbers"] > 1).sum()) if len(ag) else 0,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
