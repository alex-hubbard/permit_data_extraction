#!/usr/bin/env python3
"""
Extract permit files from EPA "all_files" ZIP downloads.

Defaults:
- input:  data/raw/epa_final_permits
- output: data/raw/epa_final_permits/unzipped
- extracts PDFs only (use --all-files for everything)
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path
from typing import Iterable, Tuple

from loguru import logger

from permit_data_extraction.config import RAW_DATA_DIR


def iter_zip_files(input_dir: Path) -> Iterable[Path]:
    yield from sorted(input_dir.rglob("*.zip"))


def _is_safe_member(member: Path) -> bool:
    if member.is_absolute():
        return False
    if any(part == ".." for part in member.parts):
        return False
    return True


def _prepare_output_dir(output_root: Path, input_root: Path, zip_path: Path) -> Path:
    relative_parent = zip_path.parent.relative_to(input_root)
    return output_root / relative_parent / zip_path.stem


def _resolve_safe_path(base_dir: Path, relative_path: Path) -> Path:
    target = (base_dir / relative_path).resolve()
    base_resolved = base_dir.resolve()
    if base_resolved == target or base_resolved in target.parents:
        return target
    raise ValueError("Unsafe path detected in ZIP entry")


def _iter_zip_members(zip_file: zipfile.ZipFile) -> Iterable[Tuple[zipfile.ZipInfo, Path]]:
    for info in zip_file.infolist():
        if info.is_dir():
            continue
        member_path = Path(info.filename)
        if not _is_safe_member(member_path):
            logger.warning(f"Skipping unsafe member path: {info.filename}")
            continue
        yield info, member_path


def extract_zip(
    zip_path: Path,
    output_root: Path,
    input_root: Path,
    pdf_only: bool,
    overwrite: bool,
) -> Tuple[int, int, int]:
    extracted = 0
    skipped_existing = 0
    skipped_non_pdf = 0

    output_dir = _prepare_output_dir(output_root, input_root, zip_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zip_file:
        for info, member_path in _iter_zip_members(zip_file):
            if pdf_only and member_path.suffix.lower() != ".pdf":
                skipped_non_pdf += 1
                continue

            try:
                target_path = _resolve_safe_path(output_dir, member_path)
            except ValueError:
                logger.warning(f"Skipping unsafe extraction target: {member_path}")
                continue

            if target_path.exists() and not overwrite:
                skipped_existing += 1
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(info) as source, target_path.open("wb") as dest:
                dest.write(source.read())
            extracted += 1

    return extracted, skipped_existing, skipped_non_pdf


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process EPA ZIP permits into PDFs.")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=RAW_DATA_DIR / "epa_final_permits",
        help="Directory containing EPA ZIP downloads.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=RAW_DATA_DIR / "epa_final_permits" / "unzipped",
        help="Directory to write extracted files.",
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Extract all files (default: PDFs only).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite files that already exist.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir: Path = args.input_dir
    output_dir: Path = args.output_dir
    pdf_only = not args.all_files

    if not input_dir.exists():
        logger.error(f"Input directory not found: {input_dir}")
        return 1

    zip_files = list(iter_zip_files(input_dir))
    if not zip_files:
        logger.warning(f"No ZIP files found under: {input_dir}")
        return 0

    logger.info(f"Found {len(zip_files)} ZIP files under {input_dir}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Mode: {'PDFs only' if pdf_only else 'all files'}")

    totals = {
        "zips_processed": 0,
        "files_extracted": 0,
        "skipped_existing": 0,
        "skipped_non_pdf": 0,
        "failed": 0,
    }

    for zip_path in zip_files:
        logger.info(f"Processing ZIP: {zip_path.relative_to(input_dir)}")
        try:
            extracted, skipped_existing, skipped_non_pdf = extract_zip(
                zip_path,
                output_dir,
                input_dir,
                pdf_only=pdf_only,
                overwrite=args.overwrite,
            )
            totals["zips_processed"] += 1
            totals["files_extracted"] += extracted
            totals["skipped_existing"] += skipped_existing
            totals["skipped_non_pdf"] += skipped_non_pdf
        except zipfile.BadZipFile:
            totals["failed"] += 1
            logger.warning(f"Bad ZIP file (skipping): {zip_path}")
        except Exception as exc:
            totals["failed"] += 1
            logger.warning(f"Error processing {zip_path}: {exc}")

    logger.info("Extraction summary")
    logger.info(f"ZIP files processed: {totals['zips_processed']}")
    logger.info(f"Files extracted: {totals['files_extracted']}")
    logger.info(f"Skipped existing: {totals['skipped_existing']}")
    if pdf_only:
        logger.info(f"Skipped non-PDF: {totals['skipped_non_pdf']}")
    logger.info(f"Failed ZIPs: {totals['failed']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
