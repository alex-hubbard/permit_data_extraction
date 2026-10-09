#!/usr/bin/env python3
"""Stage the public download files for the dashboard and upload them to GCS.

The dashboard used to build download files inside the Cloud Run container
(copy the filtered frame, write Parquet/CSV in memory). On the 4 GiB instance
that OOMs for any large selection, and Cloud Run caps a single response at
32 MB anyway. The files now live in a public bucket and the dashboard links to
them; only small selections (<= CSV_MAX_ROWS rows) are still built on demand.

Staged layout (``--stage`` dir, mirrored to ``gs://<bucket>/<prefix>/``):

    permit_units_<ver>_full.parquet        release parquet, original columns
    permit_units_<ver>_full.csv.gz         release union CSV, gzip
    subsets/permit_units_<ver>_<code>.parquet
        one file per dashboard dropdown option (MFG, DATA_CENTER, WATER,
        WASTEWATER, every 3-digit NAICS with >= MIN_SUBSECTOR_ROWS rows), with
        the same columns the dashboard's "Download this selection" produced
    manifest.json, README.txt

Everything streams in 100k-row batches, so it runs in < 1 GB of RAM.

Usage (from the repo root):
    PYTHONPATH=. python scripts/publish_release_to_gcs.py --build
    PYTHONPATH=. python scripts/publish_release_to_gcs.py --upload
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
for p in (str(REPO), str(REPO / "dashboards")):
    if p not in sys.path:
        sys.path.insert(0, p)
import manufacturing_subsector as dash  # noqa: E402

BUCKET = "permit-data-extraction-public"
VERSION = "v5f"
READY_PARQUET = REPO / "data/processed/dashboard/permits.parquet"
FULL_PARQUET = REPO / "data/processed/dashboard/permits_v5f.parquet"
UNION_CSV = REPO / "data/processed/permit_data_union_v5f.csv"
STAGE = REPO / "data/processed/dashboard/public_release"
BATCH = 100_000


def export_columns_from_schema(names: list[str]) -> list[str]:
    lead = [c for c in dash._LEAD_COLUMNS if c in names]
    rest = [c for c in names if c not in lead and c not in dash._EXPORT_EXCLUDE]
    return lead + rest


def option_codes() -> list[str]:
    """Dropdown codes, same rule as dash.subsector_options (minus ALL)."""
    sub = pq.read_table(READY_PARQUET, columns=["Subsector"]).column(0).to_pandas()
    counts = sub.value_counts()
    codes = [dash.MFG_CODE, dash.DATA_CENTER_CODE, dash.WATER_CODE, dash.WASTEWATER_CODE]
    for code, count in counts.items():
        if not isinstance(code, str) or not code:
            continue
        if count < dash.MIN_SUBSECTOR_ROWS:
            break
        codes.append(code)
    return codes


def _plain_schema(schema: pa.Schema) -> pa.Schema:
    """Dictionary columns -> plain strings so each subset file carries only
    the values it uses (a filtered dictionary array keeps the whole dictionary)."""
    fields = []
    for f in schema:
        if pa.types.is_dictionary(f.type):
            fields.append(pa.field(f.name, pa.string()))
        else:
            fields.append(f)
    return pa.schema(fields)


def _mask(batch: pa.RecordBatch, code: str) -> pa.Array:
    if code == dash.MFG_CODE:
        sub = batch.column("Subsector").cast(pa.string())
        return pc.fill_null(pc.is_in(sub, value_set=pa.array(list(dash.NAICS_SUBSECTOR_LABELS))), False)
    if code in dash.KEYWORD_FILTERS:
        return pc.fill_null(batch.column(f"is_{code}").cast(pa.bool_()), False)
    sub = batch.column("Subsector").cast(pa.string())
    return pc.fill_null(pc.equal(sub, code), False)


def build_subsets(stage: Path) -> list[dict]:
    pf = pq.ParquetFile(READY_PARQUET)
    names = pf.schema_arrow.names
    cols = export_columns_from_schema(names)
    need = sorted(set(cols) | {"Subsector"} | {f"is_{c}" for c in dash.KEYWORD_FILTERS if f"is_{c}" in names})
    codes = option_codes()
    out_dir = stage / "subsets"
    out_dir.mkdir(parents=True, exist_ok=True)
    writers: dict[str, pq.ParquetWriter] = {}
    rows = {c: 0 for c in codes}
    target = None
    scanned = 0
    print(f"subsets: {len(codes)} options, {len(cols)} export columns")
    for i, batch in enumerate(pf.iter_batches(batch_size=BATCH, columns=need)):
        scanned += batch.num_rows
        for code in codes:
            m = _mask(batch, code)
            if not pc.any(m).as_py():
                continue
            part = pa.Table.from_batches([batch.filter(m)]).select(cols)
            if target is None:
                target = _plain_schema(part.schema)
            part = part.cast(target)
            if code not in writers:
                writers[code] = pq.ParquetWriter(out_dir / f"permit_units_{VERSION}_{code}.parquet", target,
                                                 compression="zstd", use_dictionary=True)
            writers[code].write_table(part)
            rows[code] += part.num_rows
        print(f"  batch {i + 1}: {scanned:,} rows scanned", flush=True)
    for w in writers.values():
        w.close()
    items = []
    for code in codes:
        p = out_dir / f"permit_units_{VERSION}_{code}.parquet"
        if p.exists():
            items.append({"path": f"subsets/{p.name}", "code": code, "rows": rows[code], "bytes": p.stat().st_size})
    return items


def build(stage: Path) -> None:
    stage.mkdir(parents=True, exist_ok=True)
    items = []
    full = stage / f"permit_units_{VERSION}_full.parquet"
    shutil.copyfile(FULL_PARQUET, full)
    items.append({"path": full.name, "code": "ALL", "rows": pq.ParquetFile(full).metadata.num_rows,
                  "bytes": full.stat().st_size, "format": "parquet"})
    gz = stage / f"permit_units_{VERSION}_full.csv.gz"
    print(f"gzip {UNION_CSV.name} -> {gz.name} ...", flush=True)
    with gz.open("wb") as out:
        subprocess.run(["gzip", "-c", "-6", str(UNION_CSV)], stdout=out, check=True)
    items.append({"path": gz.name, "code": "ALL", "rows": None, "bytes": gz.stat().st_size, "format": "csv.gz"})
    for it in build_subsets(stage):
        it["format"] = "parquet"
        items.append(it)
    manifest = {"version": VERSION, "bucket": BUCKET,
                "base_url": f"https://storage.googleapis.com/{BUCKET}/{VERSION}", "files": items}
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (stage / "README.txt").write_text(
        f"Air permit emission-unit dataset, release {VERSION}\n\n"
        "permit_units_*_full.parquet : every extracted emission unit, original columns\n"
        "permit_units_*_full.csv.gz  : the same table as gzip-compressed CSV (UTF-8, CRLF)\n"
        "subsets/                    : one Parquet per dashboard subsector option, with the\n"
        "                              dashboard's export columns (adds NAICS_clean, Subsector,\n"
        "                              normalised capacity/fuel/unit-type fields)\n"
        "Equipment Category / Equipment Subcategory / Combustion Source are rule-based rollups of\n"
        "the free-text Unit Type (scripts/classify_unit_types.py in the project repository).\n")
    for it in items:
        print(f"  {it['path']:<55} {it['bytes'] / 1e6:8.1f} MB  rows={it['rows']}")
    print(f"staged {len(items)} files in {stage}")


def upload(stage: Path) -> None:
    dest = f"gs://{BUCKET}/{VERSION}/"
    cmd = ["gcloud", "storage", "rsync", "--recursive", "--cache-control=public, max-age=3600", str(stage), dest]
    print(" ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    print(f"uploaded to {dest}")
    print(f"public base URL: https://storage.googleapis.com/{BUCKET}/{VERSION}/")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="stage the files locally")
    ap.add_argument("--upload", action="store_true", help="rsync the staged files to the bucket")
    ap.add_argument("--stage", type=Path, default=STAGE)
    args = ap.parse_args()
    if not (args.build or args.upload):
        ap.error("pass --build and/or --upload")
    if args.build:
        build(args.stage)
    if args.upload:
        upload(args.stage)


if __name__ == "__main__":
    main()
