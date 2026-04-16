#!/usr/bin/env python3
"""
Download New Mexico Environment Department Air Quality Bureau permit documents
from the public notices page:
https://www.env.nm.gov/public-notices/

This script intentionally skips water-quality related notices and downloads.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
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

PAGE_URL = "https://www.env.nm.gov/public-notices/"
DEFAULT_TIMEOUT = 90

AIR_MARKER_RE = re.compile(r"Public\s*Notice\s*[–-]\s*Air\s*Quality\s*Bureau", re.IGNORECASE)
TITLE_V_RE = re.compile(r"title\s*v", re.IGNORECASE)

SKIP_TEXT_RE = re.compile(
    r"(surface water|ground water|groundwater|wastewater|npdes|401 certification|discharge permit|"
    r"water quality|public comment portal|events calendar|docketed matters)",
    re.IGNORECASE,
)

ALLOW_TEXT_RE = re.compile(
    r"(air quality|title\s*v|operating permit|construction permit|new source review|"
    r"permit application|draft permit|statement of basis|database summary|public involvement plan)",
    re.IGNORECASE,
)

EXCLUDE_LABEL_RE = re.compile(
    r"(public\s*notice|aviso\s*p[úu]blico|spanish|espa[ñn]ol)",
    re.IGNORECASE,
)


def _candidate_containers(soup: BeautifulSoup) -> Iterable:
    """
    Yield likely container nodes that correspond to air-quality notice blocks.
    """
    seen_ids: Set[int] = set()
    for marker in soup.find_all(string=AIR_MARKER_RE):
        node = marker
        for _ in range(6):
            if not getattr(node, "parent", None):
                break
            node = node.parent
            if getattr(node, "name", None) in {"li", "div", "section", "article"}:
                node_id = id(node)
                if node_id not in seen_ids:
                    seen_ids.add(node_id)
                    yield node
                break


def _looks_like_air_doc(link_text: str, href: str, context_text: str) -> bool:
    combined = f"{link_text} {href} {context_text}".strip()
    label_and_url = f"{link_text} {href}".strip()
    if not combined:
        return False
    # Only exclude when the link label/URL itself is a notice/spanish artifact.
    # Context often includes "Public Notice - Air Quality Bureau" for all links.
    if EXCLUDE_LABEL_RE.search(label_and_url):
        return False
    if SKIP_TEXT_RE.search(combined) and not TITLE_V_RE.search(combined):
        return False
    return bool(ALLOW_TEXT_RE.search(combined) or TITLE_V_RE.search(combined))


def collect_air_links(page_url: str) -> List[Dict[str, str]]:
    resp = requests.get(page_url, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    found: List[Dict[str, str]] = []
    seen_urls: Set[str] = set()

    for container in _candidate_containers(soup):
        context_text = container.get_text(" ", strip=True)
        for anchor in container.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith("#"):
                continue
            if href.lower().startswith(("mailto:", "tel:", "javascript:")):
                continue
            abs_url = urljoin(page_url, href)
            if abs_url == page_url or abs_url in seen_urls:
                continue
            text = anchor.get_text(" ", strip=True)
            if not _looks_like_air_doc(text, abs_url, context_text):
                continue

            seen_urls.add(abs_url)
            found.append(
                {
                    "url": abs_url,
                    "link_text": text,
                    "context_excerpt": context_text[:280],
                }
            )

    # Extra pass: catch title-v links anywhere on the page that are not water-quality.
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        abs_url = urljoin(page_url, href)
        if abs_url in seen_urls:
            continue
        text = anchor.get_text(" ", strip=True)
        surrounding = anchor.parent.get_text(" ", strip=True) if anchor.parent else ""
        combined = f"{text} {surrounding} {abs_url}"
        if (
            TITLE_V_RE.search(combined)
            and not SKIP_TEXT_RE.search(combined)
            and not EXCLUDE_LABEL_RE.search(combined)
        ):
            seen_urls.add(abs_url)
            found.append(
                {
                    "url": abs_url,
                    "link_text": text,
                    "context_excerpt": surrounding[:280],
                }
            )

    return found


def _filename_from_response(url: str, response: requests.Response, fallback_hint: str) -> str:
    cd = response.headers.get("content-disposition", "")
    if "filename=" in cd.lower():
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, flags=re.IGNORECASE)
        if match:
            name = clean_filename(match.group(1).strip()) or ""
            if name:
                return name

    path_name = Path(urlparse(url).path).name
    if path_name:
        return clean_filename(path_name) or clean_filename(fallback_hint) or "document"

    content_type = (response.headers.get("content-type") or "").lower()
    if "pdf" in content_type:
        ext = ".pdf"
    elif "msword" in content_type or "wordprocessingml" in content_type:
        ext = ".docx"
    elif "spreadsheet" in content_type or "excel" in content_type:
        ext = ".xlsx"
    else:
        ext = ".bin"
    stem = clean_filename(fallback_hint) or "document"
    return f"{stem}{ext}"


def download_links(
    links: List[Dict[str, str]],
    output_dir: Path,
    *,
    skip_existing: bool,
    dry_run: bool,
) -> List[Dict[str, str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    index_rows: List[Dict[str, str]] = []
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Referer": PAGE_URL,
        }
    )

    for i, item in enumerate(links, start=1):
        url = item["url"]
        link_text = item.get("link_text", "")
        logger.info(f"[{i}/{len(links)}] {link_text or '(no text)'} -> {url}")

        if dry_run:
            index_rows.append(
                {
                    "url": url,
                    "link_text": link_text,
                    "status": "dry_run",
                    "local_path": "",
                    "content_type": "",
                }
            )
            continue

        try:
            resp = session.get(url, timeout=DEFAULT_TIMEOUT, stream=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(f"Request failed: {exc}")
            index_rows.append(
                {
                    "url": url,
                    "link_text": link_text,
                    "status": "failed_request",
                    "local_path": "",
                    "content_type": "",
                }
            )
            continue

        content_type = (resp.headers.get("content-type") or "").lower()
        if "text/html" in content_type:
            index_rows.append(
                {
                    "url": url,
                    "link_text": link_text,
                    "status": "skipped_html",
                    "local_path": "",
                    "content_type": content_type,
                }
            )
            continue

        fallback_hint = f"nm_air_{i}_{link_text[:80]}".strip("_")
        filename = _filename_from_response(url, resp, fallback_hint=fallback_hint)
        if "." not in Path(filename).name:
            if "pdf" in content_type:
                filename = f"{filename}.pdf"
            elif "zip" in content_type:
                filename = f"{filename}.zip"

        dest = output_dir / filename
        if skip_existing and dest.exists():
            index_rows.append(
                {
                    "url": url,
                    "link_text": link_text,
                    "status": "skipped_existing",
                    "local_path": str(dest),
                    "content_type": content_type,
                }
            )
            continue

        try:
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
            status = "downloaded"
        except Exception as exc:
            logger.warning(f"Write failed: {exc}")
            status = "failed_write"

        index_rows.append(
            {
                "url": url,
                "link_text": link_text,
                "status": status,
                "local_path": str(dest) if status == "downloaded" else "",
                "content_type": content_type,
            }
        )

    return index_rows


def write_index(index_csv: Path, rows: List[Dict[str, str]]) -> None:
    index_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["url", "link_text", "status", "local_path", "content_type"]
    with index_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Download New Mexico Air Quality Bureau permit documents and "
            "Title V applications from the NMED public notices page."
        )
    )
    default_output = RAW_DATA_DIR / "nm_env_air_quality_permits"
    default_index = default_output / "nm_env_air_quality_permits_index.csv"
    parser.add_argument(
        "--page-url",
        type=str,
        default=PAGE_URL,
        help=f"Public notices URL (default: {PAGE_URL}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help=f"Directory to save downloaded files (default: {default_output}).",
    )
    parser.add_argument(
        "--index-csv",
        type=Path,
        default=default_index,
        help=f"CSV index path (default: {default_index}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and print links without downloading files.",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-download files even if destination exists.",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    index_csv = args.index_csv.expanduser().resolve()

    links = collect_air_links(args.page_url)
    logger.info(f"Collected {len(links)} candidate Air Quality/Title V links.")

    rows = download_links(
        links,
        output_dir,
        skip_existing=not args.no_skip_existing,
        dry_run=args.dry_run,
    )
    write_index(index_csv, rows)

    downloaded = sum(1 for r in rows if r["status"] == "downloaded")
    skipped = sum(1 for r in rows if r["status"].startswith("skipped"))
    failed = sum(1 for r in rows if r["status"].startswith("failed"))
    logger.info("=" * 60)
    logger.info("NM AIR QUALITY DOWNLOAD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Candidates: {len(links)}")
    logger.info(f"Downloaded: {downloaded}")
    logger.info(f"Skipped: {skipped}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Output: {output_dir}")
    logger.info(f"Index CSV: {index_csv}")


if __name__ == "__main__":
    main()
