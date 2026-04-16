#!/usr/bin/env python3
"""
Download Idaho DEQ issued permit documents for Type == "Air - Tier I".

Source page:
https://www.deq.idaho.gov/permits/issued-permits-and-water-quality-certifications/

The page contains a large table of permit records and document links. This script:
1) Parses rows from the table.
2) Filters rows to Type "Air - Tier I".
3) Collects document links from both Expiration and Document(s) columns.
4) Resolves LEIA folder pages into direct document download links.
5) Downloads files and writes an index CSV.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

# Ensure repository root is importable when executed as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

INDEX_URL = (
    "https://www.deq.idaho.gov/permits/issued-permits-and-water-quality-certifications/"
)
DEFAULT_TIMEOUT = 90
DIRECT_DOWNLOAD_MARKER = "/admin/LEIA/api/document/download/"


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    return session


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def normalize_type(value: str) -> str:
    value = (value or "").replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", value).strip().lower()


def parse_table_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("Could not find permit table on Idaho DEQ page.")

    header_cells = table.select("thead tr th")
    if not header_cells:
        first_row = table.find("tr")
        if first_row is None:
            raise RuntimeError("Could not find table header row.")
        header_cells = first_row.find_all(["th", "td"])

    headers = [normalize_header(h.get_text(" ", strip=True)) for h in header_cells]
    if not headers:
        raise RuntimeError("Could not parse table headers.")

    idx_permittee = next((i for i, h in enumerate(headers) if "permittee" in h), None)
    idx_number = next(
        (i for i, h in enumerate(headers) if "number" in h or "project" in h), None
    )
    idx_type = next((i for i, h in enumerate(headers) if h == "type"), None)
    idx_effective = next((i for i, h in enumerate(headers) if "effective" in h), None)
    idx_expiration = next((i for i, h in enumerate(headers) if "expiration" in h), None)
    idx_water = next((i for i, h in enumerate(headers) if "water body" in h), None)
    idx_docs = next((i for i, h in enumerate(headers) if "document" in h), None)

    if idx_type is None:
        raise RuntimeError("Could not locate Type column in table.")

    tbody_rows = table.select("tbody tr")
    if not tbody_rows:
        all_rows = table.find_all("tr")
        tbody_rows = all_rows[1:] if len(all_rows) > 1 else []

    parsed: list[dict] = []

    for row in tbody_rows:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue

        def _text(i: int | None) -> str:
            if i is None or i >= len(cells):
                return ""
            return cells[i].get_text(" ", strip=True)

        type_text = _text(idx_type)
        if not type_text:
            continue

        links: list[dict] = []
        for col_name, idx in [("expiration", idx_expiration), ("documents", idx_docs)]:
            if idx is None or idx >= len(cells):
                continue
            for a in cells[idx].find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                    continue
                links.append(
                    {
                        "column": col_name,
                        "label": a.get_text(" ", strip=True) or col_name,
                        "url": urljoin(INDEX_URL, href),
                    }
                )

        parsed.append(
            {
                "permittee": _text(idx_permittee),
                "number_or_project": _text(idx_number),
                "type": type_text,
                "effective": _text(idx_effective),
                "expiration": _text(idx_expiration),
                "water_body": _text(idx_water),
                "links": links,
            }
        )

    return parsed


def is_direct_download(url: str) -> bool:
    return DIRECT_DOWNLOAD_MARKER.lower() in url.lower()


def looks_like_file_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip"))


def discover_document_urls(session: requests.Session, url: str) -> list[str]:
    if is_direct_download(url) or looks_like_file_url(url):
        return [url]

    try:
        resp = session.get(url, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning(f"Failed to load linked page {url}: {exc}")
        return []

    content_type = (resp.headers.get("content-type") or "").lower()
    if "text/html" not in content_type:
        return [url]

    soup = BeautifulSoup(resp.text, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        abs_url = urljoin(url, href)
        if not (is_direct_download(abs_url) or looks_like_file_url(abs_url)):
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        found.append(abs_url)
    return found


def safe_stem(*parts: str) -> str:
    text = "_".join(p for p in parts if p).strip("_")
    text = clean_filename(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "id_deq_document"


def filename_from_response(url: str, response: requests.Response, fallback_stem: str) -> str:
    cd = response.headers.get("content-disposition", "")
    if "filename=" in cd.lower():
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, flags=re.IGNORECASE)
        if m:
            name = clean_filename(m.group(1).strip())
            if name:
                return name

    path_name = Path(urlparse(url).path).name
    if path_name:
        cleaned = clean_filename(path_name)
        if cleaned:
            return cleaned

    content_type = (response.headers.get("content-type") or "").lower()
    if "pdf" in content_type:
        ext = ".pdf"
    elif "word" in content_type:
        ext = ".docx"
    elif "excel" in content_type or "spreadsheet" in content_type:
        ext = ".xlsx"
    elif "zip" in content_type:
        ext = ".zip"
    else:
        ext = ".bin"
    return f"{fallback_stem}{ext}"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    idx = 2
    while True:
        candidate = path.with_name(f"{stem}_{idx}{suffix}")
        if not candidate.exists():
            return candidate
        idx += 1


def iter_filtered_rows(rows: Iterable[dict], target_type: str) -> list[dict]:
    target = normalize_type(target_type)
    return [row for row in rows if normalize_type(row.get("type", "")) == target]


def write_index_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "permittee",
        "number_or_project",
        "type",
        "effective",
        "expiration",
        "water_body",
        "source_column",
        "source_label",
        "source_url",
        "resolved_url",
        "status",
        "local_path",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Wrote index CSV: {path}")


def download_idaho_air_tier1(
    output_dir: Path,
    index_csv: Path,
    *,
    max_rows: int | None,
    sleep_seconds: float,
    skip_existing: bool,
    dry_run: bool,
) -> None:
    session = create_session()
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Loading Idaho DEQ permit index: {INDEX_URL}")
    resp = session.get(INDEX_URL, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()

    all_rows = parse_table_rows(resp.text)
    tier1_rows = iter_filtered_rows(all_rows, "Air - Tier I")
    if max_rows is not None:
        tier1_rows = tier1_rows[:max_rows]

    logger.info(
        f"Parsed {len(all_rows)} rows total; {len(tier1_rows)} rows match Air - Tier I."
    )

    index_rows: list[dict] = []
    downloaded = 0
    skipped = 0
    failed = 0

    for row_idx, row in enumerate(tier1_rows, start=1):
        links = row.get("links", [])
        if not links:
            index_rows.append(
                {
                    **{k: row.get(k, "") for k in ("permittee", "number_or_project", "type", "effective", "expiration", "water_body")},
                    "source_column": "",
                    "source_label": "",
                    "source_url": "",
                    "resolved_url": "",
                    "status": "no_links",
                    "local_path": "",
                    "error": "",
                }
            )
            continue

        for source in links:
            resolved_urls = discover_document_urls(session, source["url"])
            if not resolved_urls:
                index_rows.append(
                    {
                        **{k: row.get(k, "") for k in ("permittee", "number_or_project", "type", "effective", "expiration", "water_body")},
                        "source_column": source["column"],
                        "source_label": source["label"],
                        "source_url": source["url"],
                        "resolved_url": "",
                        "status": "no_resolved_docs",
                        "local_path": "",
                        "error": "",
                    }
                )
                continue

            for doc_url in resolved_urls:
                logger.info(
                    f"[{row_idx}/{len(tier1_rows)}] {row.get('number_or_project', '')} -> {doc_url}"
                )
                base_stem = safe_stem(
                    row.get("number_or_project", ""),
                    row.get("permittee", ""),
                    source.get("label", ""),
                )

                if dry_run:
                    index_rows.append(
                        {
                            **{k: row.get(k, "") for k in ("permittee", "number_or_project", "type", "effective", "expiration", "water_body")},
                            "source_column": source["column"],
                            "source_label": source["label"],
                            "source_url": source["url"],
                            "resolved_url": doc_url,
                            "status": "dry_run",
                            "local_path": "",
                            "error": "",
                        }
                    )
                    continue

                try:
                    r = session.get(doc_url, timeout=DEFAULT_TIMEOUT, stream=True, headers={"Referer": source["url"]})
                    r.raise_for_status()
                    filename = filename_from_response(doc_url, r, fallback_stem=base_stem)
                    dest = output_dir / filename
                    if skip_existing and dest.exists():
                        skipped += 1
                        index_rows.append(
                            {
                                **{k: row.get(k, "") for k in ("permittee", "number_or_project", "type", "effective", "expiration", "water_body")},
                                "source_column": source["column"],
                                "source_label": source["label"],
                                "source_url": source["url"],
                                "resolved_url": doc_url,
                                "status": "skipped_existing",
                                "local_path": str(dest),
                                "error": "",
                            }
                        )
                        continue

                    dest = unique_path(dest) if dest.exists() else dest
                    with dest.open("wb") as f:
                        for chunk in r.iter_content(chunk_size=64 * 1024):
                            if chunk:
                                f.write(chunk)
                    downloaded += 1
                    index_rows.append(
                        {
                            **{k: row.get(k, "") for k in ("permittee", "number_or_project", "type", "effective", "expiration", "water_body")},
                            "source_column": source["column"],
                            "source_label": source["label"],
                            "source_url": source["url"],
                            "resolved_url": doc_url,
                            "status": "downloaded",
                            "local_path": str(dest),
                            "error": "",
                        }
                    )
                except Exception as exc:
                    failed += 1
                    index_rows.append(
                        {
                            **{k: row.get(k, "") for k in ("permittee", "number_or_project", "type", "effective", "expiration", "water_body")},
                            "source_column": source["column"],
                            "source_label": source["label"],
                            "source_url": source["url"],
                            "resolved_url": doc_url,
                            "status": "failed_download",
                            "local_path": "",
                            "error": str(exc),
                        }
                    )
                    logger.warning(f"Download failed for {doc_url}: {exc}")

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

    write_index_csv(index_csv, index_rows)
    logger.info(
        f"Done. downloaded={downloaded}, skipped={skipped}, failed={failed}, index_rows={len(index_rows)}"
    )
    logger.info(f"Output directory: {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Idaho DEQ permit docs filtered to Type 'Air - Tier I'."
    )
    default_output_dir = RAW_DATA_DIR / "id_deq_air_tier1_permits"
    default_index_csv = default_output_dir / "id_deq_air_tier1_permits_index.csv"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output_dir,
        help=f"Directory to save downloaded files (default: {default_output_dir}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index_csv,
        help=f"Path for index CSV (default: {default_index_csv}).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Optional cap on number of Air - Tier I rows to process.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.2,
        help="Delay between downloads to reduce request bursts.",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Redownload files even when destination file already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse rows and links without downloading files.",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    index_csv = args.index_csv.expanduser().resolve()

    download_idaho_air_tier1(
        output_dir=output_dir,
        index_csv=index_csv,
        max_rows=args.max_rows,
        sleep_seconds=args.sleep_seconds,
        skip_existing=not args.no_skip_existing,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
