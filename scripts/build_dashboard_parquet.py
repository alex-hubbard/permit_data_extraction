"""Convert dashboard inputs to compact parquet artifacts.

Reads:
    data/processed/permit_data_extracted.xlsx  (both sheets)
    data/external/FRS_FACILITIES.csv

Writes:
    data/processed/dashboard/permits.parquet
    data/processed/dashboard/city_centroids.parquet

Optionally uploads the produced files to S3 with --upload-to s3://bucket/prefix/.
The dashboard reads from the same S3 prefix when PERMIT_DASHBOARD_S3_URI is set.

Re-run after dataset updates.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_XLSX = PROJECT_ROOT / "data" / "processed" / "permit_data_extracted.xlsx"
DEFAULT_FRS = PROJECT_ROOT / "data" / "external" / "FRS_FACILITIES.csv"
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "processed" / "dashboard"
PERMITS_NAME = "permits.parquet"
CENTROIDS_NAME = "city_centroids.parquet"

SHEET_NAMES = ("Manufacturing NAICS 31-33", "Other NAICS")


def build_permits_parquet(xlsx: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for sheet in SHEET_NAMES:
        try:
            df = pd.read_excel(xlsx, sheet_name=sheet, dtype=str)
        except ValueError:
            print(f"  [skip] sheet not found: {sheet}")
            continue
        df["Source Sheet"] = sheet
        print(f"  [load] {sheet}: {len(df):,} rows")
        frames.append(df)
    if not frames:
        raise SystemExit(f"No sheets read from {xlsx}")
    combined = pd.concat(frames, ignore_index=True)
    combined.to_parquet(out_path, index=False, compression="zstd")
    print(f"  [write] {out_path}  ({out_path.stat().st_size / 1e6:.1f} MB, {len(combined):,} rows)")


def build_centroids_parquet(frs: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["FAC_CITY", "FAC_STATE", "LATITUDE_MEASURE", "LONGITUDE_MEASURE"]
    df = pd.read_csv(frs, dtype=str, usecols=cols)
    df["Lat"] = pd.to_numeric(df["LATITUDE_MEASURE"], errors="coerce")
    df["Lon"] = pd.to_numeric(df["LONGITUDE_MEASURE"], errors="coerce")
    df = df.dropna(subset=["Lat", "Lon", "FAC_CITY", "FAC_STATE"])
    df = df[
        df["Lat"].between(17, 72)
        & df["Lon"].between(-180, -65)
        & (df["Lat"] != 0)
    ]
    df["key_state"] = df["FAC_STATE"].str.upper().str.strip()
    df["key_city"] = df["FAC_CITY"].str.upper().str.strip()
    centroids = (
        df.groupby(["key_state", "key_city"])[["Lat", "Lon"]].median().reset_index()
    )
    centroids.to_parquet(out_path, index=False, compression="zstd")
    print(
        f"  [write] {out_path}  "
        f"({out_path.stat().st_size / 1e6:.2f} MB, {len(centroids):,} city centroids)"
    )


def upload_to_s3(local_paths: list[Path], s3_prefix: str) -> None:
    """Upload files to s3://bucket/prefix/ using boto3 default credentials."""
    import boto3

    parsed = urlparse(s3_prefix)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise SystemExit(
            f"--upload-to must be of the form s3://bucket/prefix/, got {s3_prefix!r}"
        )
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    s3 = boto3.client("s3")
    for path in local_paths:
        if not path.exists():
            print(f"  [skip-upload] {path} does not exist")
            continue
        key = prefix + path.name
        s3.upload_file(str(path), bucket, key)
        print(f"  [upload] s3://{bucket}/{key}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--frs", type=Path, default=DEFAULT_FRS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--skip-permits", action="store_true", help="Skip rebuilding permits.parquet"
    )
    parser.add_argument(
        "--skip-centroids", action="store_true", help="Skip rebuilding city_centroids.parquet"
    )
    parser.add_argument(
        "--upload-to",
        type=str,
        default=None,
        metavar="s3://bucket/prefix/",
        help="If set, upload the produced parquet files to this S3 prefix.",
    )
    args = parser.parse_args()

    permits_path = args.out_dir / PERMITS_NAME
    centroids_path = args.out_dir / CENTROIDS_NAME

    if not args.skip_permits:
        print(f"Building permits parquet from {args.xlsx}")
        build_permits_parquet(args.xlsx, permits_path)
    if not args.skip_centroids:
        print(f"Building city centroids parquet from {args.frs}")
        build_centroids_parquet(args.frs, centroids_path)

    if args.upload_to:
        print(f"Uploading parquet artifacts to {args.upload_to}")
        upload_to_s3([permits_path, centroids_path], args.upload_to)


if __name__ == "__main__":
    main()
