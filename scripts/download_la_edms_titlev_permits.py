#!/usr/bin/env python3
"""
Download Louisiana DEQ air permit documents from EDMS v2.

LDEQ's EDMS (https://edms.deq.louisiana.gov/edmsv2) is an Angular SPA over a
JSON API (no auth, no recaptcha enforcement observed on the API itself):

  POST /edmsv2/documentSearch/filter        paged metadata search (<=500 rows)
  POST /edmsv2/document/CreateDownloadRequest  {docs: <docIDSecured>} -> jobId
  GET  /edmsv2/document/downloadStatus?jobid=  poll until hasCompleted
  GET  /app/cache/<jobid>.zip               zip containing <docid>.pdf

Notes on request shape (learned from the minified bundle):
  * filter.descriptionMode / contentSearchMode are STRING enums ("Exact");
    sending an int is a 400, omitting other fields is a 500.
  * CreateDownloadRequest requires the *secured* id (docIDSecured from search
    results — UTF-16LE hex of the numeric id), not the plain docId.

Two-stage workflow
------------------
  enumerate: page through all Air Quality "Permits"-type documents
             (Final Permit + Draft Permit subtypes by default) statewide and
             write a metadata index CSV (~45K rows, ~90 requests).
  download:  filter the index (subtype/description regex/AI-number list),
             then run the per-document job flow. Resumable via the download
             log; already-present PDFs are skipped.

Output layout
-------------
  <RAW_DATA_DIR>/la_edms_titlev_permits/
    pdfs/AI<ai>_<docid>_<date>_<desc>.pdf
    la_edms_air_permits_index.csv
    la_edms_download_log.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR

BASE = "https://edms.deq.louisiana.gov/edmsv2"
CACHE_BASE = "https://edms.deq.louisiana.gov/app/cache"

DEFAULT_OUTPUT_DIR = RAW_DATA_DIR / "la_edms_titlev_permits"
INDEX_PATH = DEFAULT_OUTPUT_DIR / "la_edms_air_permits_index.csv"
DL_LOG = DEFAULT_OUTPUT_DIR / "la_edms_download_log.csv"

DEFAULT_SUBTYPES = ["Final Permit", "Draft Permit"]
PAGE_ROWS = 500

INDEX_FIELDS = [
    "doc_id", "doc_id_secured", "ai_numbers", "description", "document_type",
    "document_subtype", "function", "media", "document_date", "entry_date",
    "pages", "activity_numbers", "batch_name", "access_rights",
    "confidential_group",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": BASE + "/advanced-search",
}


def build_session(retries: int) -> requests.Session:
    retry = Retry(
        total=retries,
        backoff_factor=2.0,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    s = requests.Session()
    s.headers.update(HEADERS)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def make_filter(subtypes: List[str]) -> Dict:
    """Full filter object — the API 500s if the nested shapes are missing."""
    return {
        "aiInformation": "",
        "documentId": None,
        "documentDateRange": {"start": None, "end": None},
        "entryDateRange": {"start": None, "end": None},
        "activityNumber": None,
        "description": None,
        "descriptionMode": "Exact",
        "descriptionFuzzy": False,
        "functions": [],
        "medias": ["Air Quality"],
        "documentTypes": ["Permits"],
        "documentSubtypes": subtypes,
        "pagesMin": None,
        "pagesMax": None,
        "accessRights": None,  # nullable int, NOT a list
        "keywords": [],
        "keywordValues": [],
        "keywordContentSearchText": None,
        "fullContentSearchText": None,
        "contentSearchMode": "Exact",
        "repositoryDocId": None,
        "pwsids": [],
    }


def search_page(session: requests.Session, flt: Dict, start: int, rows: int,
                timeout: int) -> Dict:
    body = {"filter": flt, "refinerFilter": None, "start": start, "rows": rows,
            "sort": None, "asc": False, "highlight": False}
    r = session.post(f"{BASE}/documentSearch/filter", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def row_from_doc(d: Dict) -> Dict[str, str]:
    return {
        "doc_id": str(d.get("id") or ""),
        "doc_id_secured": d.get("docIDSecured") or "",
        "ai_numbers": ";".join(str(a) for a in (d.get("aiNumbers") or [])),
        "description": (d.get("description") or "").strip(),
        "document_type": d.get("documentType") or "",
        "document_subtype": d.get("documentSubtype") or "",
        "function": d.get("function") or "",
        "media": ";".join(d.get("media") or []),
        "document_date": d.get("documentDate") or "",
        "entry_date": d.get("entryDate") or "",
        "pages": str(d.get("pages") or ""),
        "activity_numbers": ";".join(d.get("activityNumbers") or []),
        "batch_name": d.get("batchName") or "",
        "access_rights": str(d.get("accessRights") or ""),
        "confidential_group": d.get("confidentialGroup") or "",
    }


def enumerate_cmd(args: argparse.Namespace) -> int:
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = build_session(args.retries)
    flt = make_filter(args.subtypes)

    seen = set()
    rows_out: List[Dict[str, str]] = []
    start = 0
    total: Optional[int] = None
    while True:
        payload = search_page(session, flt, start, PAGE_ROWS, args.timeout)
        if total is None:
            total = int(payload.get("total") or 0)
            logger.info(f"enumeration: {total} documents match")
        data = payload.get("data") or []
        if not data:
            break
        for d in data:
            row = row_from_doc(d)
            if row["doc_id"] and row["doc_id"] not in seen:
                seen.add(row["doc_id"])
                rows_out.append(row)
        start += len(data)
        if start % 5000 < PAGE_ROWS:
            logger.info(f"  enumerated {start}/{total}")
        if total and start >= total:
            break
        if args.delay:
            time.sleep(args.delay)

    tmp = INDEX_PATH.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        w.writeheader()
        w.writerows(rows_out)
    tmp.replace(INDEX_PATH)
    logger.info(f"wrote {len(rows_out)} rows -> {INDEX_PATH}")
    return 0


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def create_download_job(session: requests.Session, secured_id: str,
                        timeout: int, ocr: bool = False) -> Dict:
    # OCR=True asks the server for a searchable PDF (LDEQ stores image-only
    # scans — locally we'd have to OCR ~everything). refreshCache mirrors the
    # viewer, which forces regeneration whenever OCR is requested.
    body = {"docs": secured_id, "OCR": ocr, "refreshCache": ocr,
            "token": None}
    r = session.post(f"{BASE}/document/CreateDownloadRequest", json=body,
                     timeout=timeout)
    r.raise_for_status()
    return r.json()


def poll_job(session: requests.Session, job_id: str, timeout: int,
             poll_interval: float, max_wait: float) -> Dict:
    waited = 0.0
    while True:
        r = session.get(f"{BASE}/document/downloadStatus", params={"jobid": job_id},
                        timeout=timeout)
        r.raise_for_status()
        status = r.json()
        if status.get("hasCompleted") or status.get("hasErrors"):
            return status
        waited += poll_interval
        if waited > max_wait:
            raise RuntimeError(f"job {job_id} not complete after {max_wait}s")
        time.sleep(poll_interval)


def fetch_job_pdf(session: requests.Session, status: Dict, timeout: int) -> bytes:
    url = status.get("downloadURL") or ""
    if not url:
        job_id = status.get("jobId") or ""
        url = f"{CACHE_BASE}/{job_id}.zip"
    # session default Accept is application/json; the cache host 406s that.
    r = session.get(url, timeout=timeout, headers={"Accept": "*/*"})
    r.raise_for_status()
    blob = r.content
    if url.lower().endswith(".zip") or blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            pdf_names = [n for n in z.namelist() if n.lower().endswith(".pdf")]
            if not pdf_names:
                raise RuntimeError(f"no pdf inside {url} ({z.namelist()})")
            return z.read(pdf_names[0])
    if blob[:5] == b"%PDF-":
        return blob
    raise RuntimeError(f"unexpected content from {url}: {blob[:60]!r}")


def pdf_filename(row: Dict[str, str]) -> str:
    ai = (row.get("ai_numbers") or "").split(";")[0] or "noai"
    date = (row.get("document_date") or "")[:10]
    desc = re.sub(r"[^A-Za-z0-9._-]+", "_", row.get("description") or "")[:60].strip("_")
    return f"AI{ai}_{row['doc_id']}_{date}_{desc}".strip("_") + ".pdf"


def load_selection(args: argparse.Namespace) -> List[Dict[str, str]]:
    with open(INDEX_PATH, newline="", encoding="utf-8") as f:
        docs = list(csv.DictReader(f))
    logger.info(f"index: {len(docs)} rows")
    if args.subtypes:
        keep = set(args.subtypes)
        docs = [d for d in docs if d["document_subtype"] in keep]
        logger.info(f"  subtype filter {sorted(keep)}: -> {len(docs)}")
    if args.description_regex:
        rx = re.compile(args.description_regex, re.I)
        docs = [d for d in docs if rx.search(d["description"] or "")]
        logger.info(f"  description regex: -> {len(docs)}")
    if args.ai_file:
        wanted = set()
        for line in Path(args.ai_file).read_text().split():
            line = line.strip()
            if line:
                wanted.add(line)
        docs = [d for d in docs
                if wanted & set((d["ai_numbers"] or "").split(";"))]
        logger.info(f"  AI-number filter ({len(wanted)} AIs): -> {len(docs)}")
    if args.max:
        docs = docs[: args.max]
    return docs


def download_cmd(args: argparse.Namespace) -> int:
    if not INDEX_PATH.exists():
        logger.error(f"index not found: {INDEX_PATH} — run `enumerate` first")
        return 2
    docs = load_selection(args)
    if not docs:
        logger.warning("nothing selected to download")
        return 0

    pdf_dir = DEFAULT_OUTPUT_DIR / args.pdf_dir
    pdf_dir.mkdir(parents=True, exist_ok=True)

    already = set()
    if DL_LOG.exists():
        with open(DL_LOG, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("status") == "downloaded":
                    already.add(r["doc_id"])

    session = build_session(args.retries)
    new_log = not DL_LOG.exists()
    n_ok = n_err = n_skip = 0
    with open(DL_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["doc_id", "ai_numbers", "description",
                                          "filename", "status", "size_bytes",
                                          "error"])
        if new_log:
            w.writeheader()
        for i, d in enumerate(docs, 1):
            if d["doc_id"] in already:
                n_skip += 1
                continue
            fname = pdf_filename(d)
            dest = pdf_dir / fname
            if dest.exists() and dest.stat().st_size > 0:
                status_s, size, err = "downloaded", dest.stat().st_size, ""
            else:
                try:
                    job = create_download_job(session, d["doc_id_secured"],
                                              args.timeout, ocr=args.ocr)
                    if job.get("hasErrors"):
                        raise RuntimeError(job.get("errorsMessage") or "job error")
                    if not job.get("hasCompleted"):
                        job = poll_job(session, job["jobId"], args.timeout,
                                       args.poll_interval, args.max_wait)
                    if job.get("hasErrors"):
                        raise RuntimeError(job.get("errorsMessage") or "job error")
                    pdf = fetch_job_pdf(session, job, args.timeout)
                    tmp = dest.with_suffix(".pdf.part")
                    tmp.write_bytes(pdf)
                    tmp.replace(dest)
                    status_s, size, err = "downloaded", len(pdf), ""
                except Exception as e:
                    status_s, size, err = "error", 0, f"{type(e).__name__}: {e}"
            w.writerow({"doc_id": d["doc_id"], "ai_numbers": d["ai_numbers"],
                        "description": d["description"], "filename": fname,
                        "status": status_s, "size_bytes": size, "error": err})
            f.flush()
            if status_s == "downloaded":
                n_ok += 1
            else:
                n_err += 1
                logger.warning(f"[{i}/{len(docs)}] {d['doc_id']}: {err}")
            if i % 100 == 0:
                logger.info(f"[{i}/{len(docs)}] ok={n_ok} err={n_err} skip={n_skip}")
            if args.delay:
                time.sleep(args.delay)
    logger.info(f"done: ok={n_ok} err={n_err} skipped(existing)={n_skip}")
    return 0 if n_err == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("enumerate", enumerate_cmd), ("download", download_cmd)]:
        p = sub.add_parser(name)
        p.add_argument("--delay", type=float, default=0.5)
        p.add_argument("--timeout", type=int, default=120)
        p.add_argument("--retries", type=int, default=5)
        p.add_argument("--subtypes", nargs="+", default=DEFAULT_SUBTYPES)
        if name == "download":
            p.add_argument("--description-regex", default=None,
                           help="Only download docs whose description matches.")
            p.add_argument("--ai-file", default=None,
                           help="File with AI numbers (whitespace-separated); "
                                "only download docs for those AIs.")
            p.add_argument("--max", type=int, default=None)
            p.add_argument("--poll-interval", type=float, default=2.0)
            p.add_argument("--max-wait", type=float, default=1800.0)
            p.add_argument("--ocr", action="store_true",
                           help="Request server-side OCR (searchable PDF). "
                                "LDEQ scans are image-only without this.")
            p.add_argument("--pdf-dir", default="pdfs",
                           help="Output subdir under the dataset dir. Use a "
                                "separate dir (e.g. pdfs_raw) for non-OCR "
                                "downloads so they aren't mistaken for "
                                "searchable copies.")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
