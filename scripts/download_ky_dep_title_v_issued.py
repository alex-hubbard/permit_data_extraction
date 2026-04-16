#!/usr/bin/env python3
"""
Download Kentucky DEP eSearch issued Title V permit PDFs (row-level).

Fetches the Air issued-approvals listing via the public WebApi, keeps rows where
ActivityType contains \"Title V\" and CurrentMilestone is \"Approval Issued\",
then for each row requests document metadata and downloads one chosen PDF per row.

Expect many rows to have no documents in api/agency/documentdetails yet; the
script records status per row so you can re-run later. Successful runs can still
reach only a fraction of ~1290 rows until the agency attaches files.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests
from loguru import logger

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

API_BASE = "https://dep.gateway.ky.gov/eSearch.WebApi/api"
ISSUED_SEARCH_URL = f"{API_BASE}/issuedApprovals/search"
DOCUMENT_DETAILS_URL = f"{API_BASE}/agency/documentdetails"
GET_DOCUMENT_URL = f"{API_BASE}/documentGenerate/getDocument"


def _issued_search_params() -> Dict[str, str]:
    return {
        "model[AgencyId]": "",
        "model[AgencyName]": "",
        "model[County]": "",
        "model[Municipality]": "",
        "model[Program]": "Air",
        "model[ActivityType]": "",
        "model[LastIssuedDays]": "",
    }


def fetch_air_issued_rows(session: requests.Session, timeout: int) -> List[Dict[str, Any]]:
    response = session.get(ISSUED_SEARCH_URL, params=_issued_search_params(), timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise TypeError(f"Expected list from issuedApprovals/search, got {type(data)}")
    return data


def filter_title_v_approval_issued(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        at = row.get("ActivityType") or ""
        if "Title V" not in at:
            continue
        if (row.get("CurrentMilestone") or "") != "Approval Issued":
            continue
        out.append(row)
    return out


def _activity_query(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "agencyId": int(row["AgencyId"]),
        "activityCode": row["ActivityCode"],
        "activityYear": int(row["ActivityYear"]),
        "activityNum": int(row["ActivityNum"]),
    }


def fetch_document_details(
    session: requests.Session, row: Dict[str, Any], timeout: int
) -> List[Dict[str, Any]]:
    params = _activity_query(row)
    response = session.get(DOCUMENT_DETAILS_URL, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise TypeError(f"Expected list from documentdetails, got {type(data)}")
    return data


def pick_final_permit_doc(docs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Prefer a permit PDF that looks like the final permit; fall back to the best guess.
    """
    if not docs:
        return None

    def sort_key(d: Dict[str, Any]) -> tuple:
        title = (d.get("DocTitle") or "").lower()
        if "final" in title:
            return (0, title)
        if "permit" in title and not any(
            x in title for x in ("statement", "executive", "summary", "basis")
        ):
            return (1, title)
        if "permit" in title:
            return (2, title)
        return (3, title)

    return sorted(docs, key=sort_key)[0]


def download_pdf(
    session: requests.Session, source_doc_id: float | int, timeout: int
) -> tuple[bytes, Optional[str]]:
    response = session.get(
        GET_DOCUMENT_URL, params={"intDocId": int(source_doc_id)}, timeout=timeout
    )
    response.raise_for_status()
    cd = response.headers.get("Content-Disposition") or ""
    fname = None
    if "filename=" in cd:
        part = cd.split("filename=", 1)[1].strip().strip('"')
        if part:
            fname = part
    return response.content, fname


def default_out_dir() -> Path:
    return RAW_DATA_DIR / "ky_issued_title_v"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=default_out_dir(),
        help=f"Output directory (default: {default_out_dir()})",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.25,
        help="Seconds to sleep between document API calls (default: 0.25)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="HTTP timeout in seconds (default: 120)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process at most N rows (testing)")
    parser.add_argument(
        "--start-row",
        type=int,
        default=1,
        metavar="N",
        help="1-based index into the filtered Title V list to start from (default: 1)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip rows whose PDF already exists on disk (by output filename).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List rows and actions without downloading or writing PDFs.",
    )
    parser.add_argument(
        "--save-listing-json",
        type=Path,
        default=None,
        help="Optional path to write the raw issuedApprovals JSON for debugging.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "ky_title_v_issued_index.csv"

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "permit-data-extraction/ky-title-v (research; +https://github.com)",
            "Accept": "application/json, application/pdf, */*",
        }
    )

    logger.info("Fetching Air issued approvals from {}", ISSUED_SEARCH_URL)
    rows = fetch_air_issued_rows(session, args.timeout)
    if args.save_listing_json:
        args.save_listing_json.parent.mkdir(parents=True, exist_ok=True)
        args.save_listing_json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        logger.info("Wrote raw listing to {}", args.save_listing_json)

    filtered = filter_title_v_approval_issued(rows)
    logger.info("Title V + Approval Issued rows: {}", len(filtered))
    if args.start_row < 1:
        raise SystemExit("--start-row must be >= 1")
    start_idx = args.start_row - 1
    if start_idx >= len(filtered):
        raise SystemExit(f"--start-row {args.start_row} is past end of list ({len(filtered)} rows)")
    filtered = filtered[start_idx:]
    if args.limit is not None:
        filtered = filtered[: args.limit]
        logger.info(
            "Processing rows {}..{} ({} rows)",
            args.start_row,
            args.start_row + len(filtered) - 1,
            len(filtered),
        )
    else:
        logger.info("Processing from row {} to end ({} rows)", args.start_row, len(filtered))

    fieldnames = [
        "row_num",
        "agency_id",
        "agency_name",
        "activity_type",
        "activity_code",
        "activity_year",
        "activity_num",
        "issued_date",
        "milestone_date",
        "detail_rows",
        "doc_title_chosen",
        "source_doc_id",
        "filename",
        "status",
        "message",
    ]

    status_counts: Dict[str, int] = {}

    with index_path.open("w", encoding="utf-8", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        row_offset = args.start_row - 1
        for j, row in enumerate(filtered):
            i = row_offset + j + 1
            base_name = (
                f"{i:04d}_agency{int(row['AgencyId'])}_"
                f"y{int(row['ActivityYear'])}_n{int(row['ActivityNum'])}"
            )
            rec: Dict[str, Any] = {
                "row_num": i,
                "agency_id": int(row["AgencyId"]),
                "agency_name": row.get("AgencyName") or "",
                "activity_type": row.get("ActivityType") or "",
                "activity_code": row.get("ActivityCode") or "",
                "activity_year": int(row["ActivityYear"]),
                "activity_num": int(row["ActivityNum"]),
                "issued_date": row.get("IssuedDate") or "",
                "milestone_date": row.get("MilestoneDate") or "",
                "detail_rows": row.get("DetailRows"),
                "doc_title_chosen": "",
                "source_doc_id": "",
                "filename": "",
                "status": "",
                "message": "",
            }

            try:
                if args.dry_run:
                    rec["status"] = "dry_run"
                    rec["message"] = "skipped download"
                    writer.writerow(rec)
                    continue

                docs = fetch_document_details(session, row, args.timeout)
                time.sleep(args.sleep)
                chosen = pick_final_permit_doc(docs)
                if chosen is None:
                    rec["status"] = "no_documents"
                    rec["message"] = "documentdetails returned empty"
                    writer.writerow(rec)
                    continue

                sid = chosen.get("SourceDocId") or chosen.get("DocId")
                if sid is None:
                    rec["status"] = "error"
                    rec["message"] = "no SourceDocId on document row"
                    writer.writerow(rec)
                    continue

                rec["doc_title_chosen"] = chosen.get("DocTitle") or ""
                rec["source_doc_id"] = str(int(sid))

                title_part = clean_filename((chosen.get("DocTitle") or "permit").replace(".pdf", ""))
                fname = f"{base_name}_{title_part}.pdf"
                if len(fname) > 200:
                    fname = f"{base_name}.pdf"
                rec["filename"] = fname

                dest = out_dir / fname
                if args.skip_existing and dest.is_file():
                    rec["status"] = "skipped_exists"
                    rec["message"] = str(dest)
                    writer.writerow(rec)
                    continue

                pdf_bytes, server_name = download_pdf(session, sid, args.timeout)
                time.sleep(args.sleep)
                if not pdf_bytes.startswith(b"%PDF"):
                    rec["status"] = "error"
                    rec["message"] = "response was not a PDF"
                    writer.writerow(rec)
                    continue

                dest.write_bytes(pdf_bytes)
                if server_name:
                    rec["message"] = server_name
                rec["status"] = "downloaded"
            except requests.RequestException as exc:
                rec["status"] = "error"
                rec["message"] = str(exc)
            except (KeyError, TypeError, ValueError) as exc:
                rec["status"] = "error"
                rec["message"] = repr(exc)

            writer.writerow(rec)
            csvfile.flush()
            st = rec.get("status") or ""
            status_counts[st] = status_counts.get(st, 0) + 1

    logger.info("Wrote index: {}", index_path)
    if status_counts:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items()))
        logger.info("Run summary: {}", parts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
