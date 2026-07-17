#!/usr/bin/env python3
"""
Download Texas TCEQ Title V (Federal Operating Permit) documents.

TCEQ exposes two complementary public web tools for air permits:

1) airperm portal — https://www2.tceq.texas.gov/airperm/index.cfm
   ColdFusion application that returns a Web Report (HTML) or ASCII text
   listing of permit applications for a date/county/status filter set.
   Provides metadata only (permit number, regulated entity, county, dates) —
   no PDFs.

2) Central File Room (CFR) Online — https://records.tceq.texas.gov/cs/idcplg
   Oracle WebCenter Content (Stellent UCM) document repository where the
   actual permit PDFs live. Title V documents are filed under
   xRecordSeries=1051 ("AIR / Federal Operating Permit").

Two-stage workflow
------------------
Stage 1 (airperm): POST tv.validate_search_criteria with date/county/status.
                   Parse the HTML or ASCII report into a list of permits.
Stage 2 (CFR):     For each permit, query TCEQ_PERFORM_SEARCH filtered by
                   xRecordSeries=1051 + Regulated Entity Name. Stream each
                   matching document via IdcService=GET_FILE.

CLI gives you three driving modes:
  * --from-date/--to-date   (drive from airperm enumeration)
  * --permits PERMIT,...    (skip airperm; fetch documents for an explicit list)
  * --regulated-entity NAME (search CFR directly by company name)

Two non-obvious request-shape requirements
------------------------------------------
* CFR search: `xInsightDocumentType` and `xMedia` must be sent as ``"0"``
  (TCEQ's "any" sentinel) — empty string is interpreted as a literal-empty
  match clause that excludes every document.
* CFR download: ``RevisionSelectionMethod=LatestReleased`` is required on
  GET_FILE — without it the service returns 503 for nearly every doc.

Output layout
-------------
  <RAW_DATA_DIR>/tx_tceq_titlev_permits/
    pdfs/<permit>_<dDocName>_<title>.pdf
    tx_tceq_titlev_permits_index.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR
from permit_data_extraction.pdf_downloader import clean_filename

AIRPERM_INDEX = "https://www2.tceq.texas.gov/airperm/index.cfm"
AIRPERM_TV_START = AIRPERM_INDEX + "?fuseaction=tv.start"

CFR_BASE = "https://records.tceq.texas.gov/cs/idcplg"
CFR_FORM_URL = CFR_BASE + "?IdcService=TCEQ_SEARCH"

TITLEV_RECORD_SERIES = "1051"  # AIR / Federal Operating Permit
IPIFY_URL = "https://api.ipify.org?format=json"

# Default dDocTitle whitelist. CFR returns many ancillary doc types per permit
# (Project File Folder, Initial Application, Administrative, etc.); restrict
# to the operating permit, public notice, and core permit document types.
ALLOWED_DOC_TITLES = (
    "Site Operating Permit",
    "Public Notices & Comments",
    "Permits",
)

DEFAULT_OUTPUT_DIR = RAW_DATA_DIR / "tx_tceq_titlev_permits"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# airperm Web Report row regex — extracts <td> cells from each <tr> inside the
# results <table>. Layout determined from observed responses; tune if column
# order changes.
AIRPERM_ROW_RE = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.S | re.I)
AIRPERM_CELL_RE = re.compile(r"<td[^>]*>(?P<val>.*?)</td>", re.S | re.I)
AIRPERM_NO_RESULTS_RE = re.compile(r"There are no\s+projects", re.I)


# ---------------------------------------------------------------------------
# session / utilities (borrowed from download_aqmd_titlev_permits.py)
# ---------------------------------------------------------------------------

def build_session(retries: int) -> requests.Session:
    retry = Retry(
        total=retries,
        backoff_factor=1.0,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def parse_date_arg(value: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            d = datetime.strptime(value, fmt).date()
            return f"{d.month}/{d.day}/{d.year}"
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Could not parse date: {value!r}")


def safe_filename(parts: Iterable[str], max_len: int = 180) -> str:
    base = "_".join(p for p in parts if p)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
    if len(base) > max_len:
        base = base[:max_len].rstrip("_")
    return base or "document"


# ---------------------------------------------------------------------------
# Stage 1: airperm Title V enumeration
# ---------------------------------------------------------------------------

def airperm_prime(session: requests.Session, timeout: int) -> None:
    """GET the Title V start page so ColdFusion sets CFID/CFTOKEN."""
    r = session.get(AIRPERM_TV_START, timeout=timeout)
    r.raise_for_status()


def airperm_search_body(
    from_date: str,
    to_date: str,
    status: str,
    date_option: str,
    county_code: str,
    output_format: str = "web",
) -> str:
    """Build the URL-encoded form body.

    The ColdFusion image-input button has the unusual name
    ``_fuseaction=tv.validate_search_criteria`` (with a literal ``=`` in the
    field name). When sent verbatim by curl/requests, the literal ``=`` must
    be percent-encoded as ``%3D`` so the server parses the image-click
    coordinate suffix (``.x``/``.y``) correctly.
    """
    params = [
        ("fuseaction", "searchprojects"),
        ("loc_cnty_name", county_code or "0"),
        ("proj_num", ""),
        ("tnrcc_region_cd", "0"),
        ("pmt_id", ""),
        ("proj_typ_txt", ""),
        ("co_name", ""),
        ("cn_ref_num_txt", ""),
        ("prim_site_acc_num", ""),
        ("rn_ref_num_txt", ""),
        ("addn_id_typ_txt", ""),
        ("status_txt", status),
        ("date_option", date_option),
        ("date_range_from", from_date if date_option != "0" else ""),
        ("date_range_to", to_date if date_option != "0" else ""),
        ("sort_dir", "asc"),
        ("program", "FOP"),
        ("order_by", "proj_num"),
        ("out_form", output_format),
    ]
    body = urlencode(params)
    # Image-input coordinate suffix; keep field name's literal "=" encoded.
    body += "&_fuseaction%3Dtv.validate_search_criteria.x=1"
    body += "&_fuseaction%3Dtv.validate_search_criteria.y=1"
    return body


def airperm_search(
    session: requests.Session,
    from_date: str,
    to_date: str,
    status: str,
    date_option: str,
    county_code: str,
    timeout: int,
    debug_path: Optional[Path] = None,
) -> str:
    body = airperm_search_body(from_date, to_date, status, date_option, county_code)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": AIRPERM_TV_START,
        "Origin": "https://www2.tceq.texas.gov",
    }
    r = session.post(AIRPERM_INDEX, data=body, headers=headers, timeout=timeout,
                     allow_redirects=True)
    r.raise_for_status()
    if "Server Error" in r.text or "catching an error" in r.text:
        if debug_path:
            debug_path.write_text(r.text, encoding="utf-8")
        raise RuntimeError(
            f"airperm POST returned an error page ({len(r.text)} bytes). "
            f"Saved to {debug_path}." if debug_path else
            "airperm POST returned an error page."
        )
    return r.text


def parse_airperm_results(html: str) -> List[Dict[str, str]]:
    """Parse the airperm Title V Web Report into a list of permit dicts.

    The TCEQ search circuit silently re-renders the empty form page when its
    server-side validator rejects the submission (e.g., date_option=0 with
    no other restrictive filters). We require the "Air Permitting Actions"
    header — only emitted on a true results page — before extracting rows.
    Returns [] when the page reports "There are no projects".
    """
    if "Air Permitting Actions" not in html:
        return []
    if AIRPERM_NO_RESULTS_RE.search(html):
        return []

    rows: List[Dict[str, str]] = []
    # Locate results region: between the "Air Permitting Actions" heading and
    # the closing of its outer <table>.
    region_match = re.search(
        r"Air Permitting Actions.*?(?P<region>.*?)(?=<div\s+id=\"footer\")",
        html, re.S | re.I,
    )
    region = region_match.group("region") if region_match else html
    table_match = re.search(
        r"<table[^>]*>(?P<inner>.*?(?:Project\s*Number|Permit\s*Number).*?)</table>",
        region, re.S | re.I,
    )
    if not table_match:
        return rows
    inner = table_match.group("inner")

    # Detect header columns from first <tr> with <th> or first <tr> with bold cells.
    headers: List[str] = []
    first_tr = re.search(r"<tr[^>]*>(?P<r>.*?)</tr>", inner, re.S | re.I)
    if first_tr:
        for th in re.finditer(r"<t[hd][^>]*>(?P<v>.*?)</t[hd]>", first_tr.group("r"), re.S | re.I):
            txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", th.group("v"))).strip()
            if txt:
                headers.append(txt)

    for tr in AIRPERM_ROW_RE.finditer(inner):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c.group("val"))).strip()
            for c in AIRPERM_CELL_RE.finditer(tr.group("row"))
        ]
        if not cells or all(not c for c in cells):
            continue
        if cells == headers:
            continue
        if headers and len(cells) >= len(headers):
            row = {h: cells[i] for i, h in enumerate(headers)}
        else:
            row = {f"col_{i}": v for i, v in enumerate(cells)}
        rows.append(_normalize_airperm_row(row))
    return rows


def _normalize_airperm_row(row: Dict[str, str]) -> Dict[str, str]:
    """Map airperm column names to our canonical schema."""
    def find(*names: str) -> str:
        for n in names:
            for k, v in row.items():
                if k.lower().replace(" ", "") == n.lower().replace(" ", ""):
                    return v
        return ""

    return {
        "permit_number":     find("Permit Number", "PermitNumber", "PMT_ID"),
        "project_number":    find("Project Number", "ProjectNumber", "PROJ_NUM"),
        "regulated_entity":  find("Regulated Entity", "RN_REF_NUM_TXT"),
        "co_name":           find("Customer Name", "Company Name", "CO_NAME"),
        "county":            find("County"),
        "region":            find("Region"),
        "cn_number":         find("CN Number", "CN_REF_NUM_TXT"),
        "account":           find("Account", "PRIM_SITE_ACC_NUM"),
        "status_airperm":    find("Status"),
        "date_received":     find("Date Received", "Received"),
        "date_complete":     find("Date Complete", "Project Complete", "Complete"),
        # Keep raw row for debugging if columns shift
        **{f"_raw_{k}": v for k, v in row.items()},
    }


# ---------------------------------------------------------------------------
# Stage 2: CFR document search and download
# ---------------------------------------------------------------------------

def _public_ip(session: requests.Session, timeout: int) -> str:
    try:
        r = session.get(IPIFY_URL, timeout=timeout)
        r.raise_for_status()
        return r.json()["ip"]
    except Exception as exc:
        logger.warning(f"Could not fetch public IP from ipify; using placeholder: {exc}")
        return "0.0.0.0"


def cfr_get_access(session: requests.Session, timeout: int) -> Tuple[str, str]:
    """Prime CFR session and obtain (accessID, clientIP).

    Mirrors the JS handshake on the CFR search page: the page first calls
    api.ipify.org, then GETs TCEQ_SEARCH_ADD_ACCESS with the resolved IP and
    stores the returned accessID in a hidden form field.
    """
    # Prime cookies on the search form.
    session.get(CFR_FORM_URL, timeout=timeout).raise_for_status()
    client_ip = _public_ip(session, timeout)
    r = session.get(
        CFR_BASE,
        params={
            "IdcService": "TCEQ_SEARCH_ADD_ACCESS",
            "clientIP": client_ip,
            "searchType": "External",
            "IsJson": "1",
        },
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()["LocalData"]
    return data["accessID"], data.get("clientIP", client_ip)


def cfr_search(
    session: requests.Session,
    access_id: str,
    client_ip: str,
    *,
    record_series: str = TITLEV_RECORD_SERIES,
    reg_entity: Optional[str] = None,
    primary_id: Optional[str] = None,
    secondary_id: Optional[str] = None,
    full_text: Optional[str] = None,
    result_count: int = 50,
    start_row: int = 1,
    timeout: int = 60,
) -> Dict:
    """Run TCEQ_PERFORM_SEARCH and return the parsed JSON dict."""
    # NOTE on xInsightDocumentType / xMedia: send "0" (TCEQ's "any" sentinel),
    # NOT empty string. An empty value is interpreted as a literal-empty match
    # clause (xInsightDocumentType <matches> ``) which excludes every document
    # in the index. This is what TCEQ's own search form sends.
    fields: List[Tuple[str, str]] = [
        ("IdcService",       "TCEQ_PERFORM_SEARCH"),
        ("xIdcProfile",      "Record"),
        ("IsExternalSearch", "1"),
        ("sortSearch",       "false"),
        ("newSearch",        "true"),
        ("xRecordSeries",    record_series or "0"),
        ("xInsightDocumentType", "0"),
        ("xMedia",           "0"),
        ("operator",         "AND"),
        ("ftx",              full_text or ""),
        ("clientIP",         client_ip),
        ("accessID",         access_id),
        ("ResultCount",      str(result_count)),
        ("StartRow",         str(start_row)),
        ("SortField",        "dInDate"),
        ("SortOrder",        "Desc"),
        ("IsJson",           "1"),
    ]

    # Up to 4 criteria rows: select{i}=fieldName, input{i}=value
    criteria: List[Tuple[str, Optional[str]]] = [
        ("xRegEntName",  reg_entity),
        ("xPrimaryID",   primary_id),
        ("xSecondaryID", secondary_id),
        ("",             None),
    ]
    for i, (sel, val) in enumerate(criteria):
        fields.append((f"select{i}", sel if val else ""))
        fields.append((f"input{i}",  val or ""))

    headers = {
        "Accept": "application/json",
        "Referer": CFR_FORM_URL,
    }
    # POST works with empty criteria, GET works with criteria — use POST for
    # robustness; if criteria are supplied and POST fails, retry as GET.
    r = session.post(CFR_BASE, data=fields, headers=headers, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if payload.get("LocalData", {}).get("StatusCode") in {"-32", -32}:
        logger.debug("CFR POST returned -32; retrying as GET")
        r = session.get(CFR_BASE, params=fields, headers=headers, timeout=timeout)
        r.raise_for_status()
        payload = r.json()
    return payload


def cfr_scan_all(
    session: requests.Session,
    access_id: str,
    client_ip: str,
    *,
    record_series: str = TITLEV_RECORD_SERIES,
    page_size: int = 200,
    timeout: int = 60,
    delay: float = 0.0,
    progress_every: int = 5000,
) -> Iterable[Dict[str, str]]:
    """Yield every document in ``record_series`` by paginating CFR search.

    Stellent UCM caps ResultCount at 200 per request, so for the ~158k Title V
    documents this issues ~790 GETs. Logs progress every ``progress_every`` rows.
    """
    start_row = 1
    seen = 0
    total: Optional[int] = None
    while True:
        payload = cfr_search(
            session, access_id, client_ip,
            record_series=record_series,
            result_count=page_size,
            start_row=start_row,
            timeout=timeout,
        )
        if total is None:
            total = int(payload.get("LocalData", {}).get("TotalRows", 0) or 0)
            logger.info(f"CFR enumeration: {total} docs in record series {record_series}")
        rows = cfr_extract_rows(payload)
        if not rows:
            break
        for r in rows:
            yield r
        seen += len(rows)
        if seen % progress_every < page_size:
            logger.info(f"  enumerated {seen}/{total}")
        start_row += len(rows)
        if total and start_row > total:
            break
        if delay:
            time.sleep(delay)


def latest_per_permit_title(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    """Keep the row with the latest ``dInDate`` per (xPrimaryID, dDocTitle).

    Documents without an xPrimaryID are kept as-is (not deduped) and keyed by
    dDocName so they don't collide with each other.
    """
    def parse_date(s: str) -> datetime:
        s = (s or "").strip()
        for fmt in ("%m/%d/%Y %I:%M%p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return datetime.min

    best: Dict[Tuple[str, str], Tuple[datetime, Dict[str, str]]] = {}
    for r in rows:
        pid = (r.get("xPrimaryID") or "").strip()
        title = (r.get("dDocTitle") or "").strip()
        if pid:
            key = (pid, title)
        else:
            key = (f"_no_pid_{r.get('dDocName','')}", title)
        d = parse_date(r.get("dInDate"))
        prev = best.get(key)
        if prev is None or d > prev[0]:
            best[key] = (d, r)
    return [r for _, r in best.values()]


def cfr_extract_rows(payload: Dict) -> List[Dict[str, str]]:
    """Pull SearchResults rows out of the TCEQ_PERFORM_SEARCH JSON shape."""
    results = payload.get("ResultSets", {}).get("SearchResults") or {}
    fields = [f.get("name") for f in results.get("fields", [])]
    rows_raw = results.get("rows") or []
    out: List[Dict[str, str]] = []
    for row in rows_raw:
        if not isinstance(row, list):
            continue
        out.append({fields[i]: row[i] for i in range(min(len(fields), len(row)))})
    return out


def cfr_total_rows(payload: Dict) -> int:
    try:
        return int(payload["LocalData"].get("TotalRows", 0) or 0)
    except (ValueError, TypeError):
        return 0


def cfr_download(
    session: requests.Session,
    doc_name: str,
    dest: Path,
    timeout: int,
) -> int:
    """Stream a PDF to ``dest`` via IdcService=GET_FILE.

    Tries Rendition=Web first, then falls back to Primary, then GET_NATIVE_FILE.
    """
    # RevisionSelectionMethod=LatestReleased is REQUIRED — without it GET_FILE
    # returns 503 for almost every document.
    base = {"dDocName": doc_name, "allowInterrupt": "1",
            "RevisionSelectionMethod": "LatestReleased"}
    candidates = [
        {**base, "IdcService": "GET_FILE", "Rendition": "Web"},
        {**base, "IdcService": "GET_FILE", "Rendition": "Primary"},
        {"IdcService": "GET_NATIVE_FILE", "dDocName": doc_name, "allowInterrupt": "1",
         "RevisionSelectionMethod": "LatestReleased"},
    ]
    last_err: Optional[Exception] = None
    tmp = dest.with_suffix(dest.suffix + ".part")
    for params in candidates:
        try:
            with session.get(
                CFR_BASE, params=params, timeout=timeout, stream=True,
                headers={"Accept": "application/pdf,*/*"},
            ) as r:
                if r.status_code == 404:
                    last_err = RuntimeError(f"{params['IdcService']} {params.get('Rendition','')} -> 404")
                    continue
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                if not ctype.lower().startswith(("application/pdf", "application/octet-stream")):
                    head = next(r.iter_content(512), b"")
                    last_err = RuntimeError(
                        f"{params['IdcService']} got Content-Type={ctype!r}, head={head[:80]!r}"
                    )
                    continue
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(64 * 1024):
                        if chunk:
                            f.write(chunk)
            tmp.rename(dest)
            return dest.stat().st_size
        except Exception as exc:
            last_err = exc
            if tmp.exists():
                tmp.unlink()
            continue
    raise last_err or RuntimeError(f"All download renditions failed for {doc_name}")


def cfr_doc_filename(permit_number: str, doc: Dict[str, str]) -> str:
    title = clean_filename(doc.get("dDocTitle") or doc.get("dDocName") or "doc")
    in_date = (doc.get("dInDate") or "").replace("/", "-").split(" ")[0]
    permit_label = (permit_number or doc.get("xPrimaryID") or "").strip()
    return safe_filename(
        [
            f"permit-{permit_label}" if permit_label else f"doc-{doc.get('dDocName','')}",
            doc.get("dDocName") or "",
            in_date,
            title[:80],
        ]
    ) + ".pdf"


# ---------------------------------------------------------------------------
# Index CSV
# ---------------------------------------------------------------------------

INDEX_FIELDS = [
    "permit_number", "project_number", "regulated_entity", "co_name",
    "county", "region", "cn_number", "account",
    "status_airperm", "date_received", "date_complete",
    "record_series", "doc_name", "doc_title", "doc_in_date",
    "document_url", "filename", "status", "size_bytes", "error",
]


def write_index(rows: List[Dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in INDEX_FIELDS})
    os.replace(tmp, path)


def read_existing_index(path: Path) -> Dict[Tuple[str, str], Dict[str, str]]:
    if not path.exists():
        return {}
    out: Dict[Tuple[str, str], Dict[str, str]] = {}
    with open(path, "r", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r.get("permit_number") or "", r.get("doc_name") or "")
            out[key] = r
    return out


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------

def _default_dates() -> Tuple[str, str]:
    today = date.today()
    start = today - timedelta(days=365)
    return f"{start.month}/{start.day}/{start.year}", f"{today.month}/{today.day}/{today.year}"


def collect_permits_from_airperm(
    session: requests.Session, args: argparse.Namespace,
) -> List[Dict[str, str]]:
    debug_path = args.output_dir / "_debug_airperm_raw.html" if args.debug else None
    airperm_prime(session, args.timeout)
    html = airperm_search(
        session,
        from_date=args.from_date,
        to_date=args.to_date,
        status=args.status,
        date_option=args.date_option,
        county_code=args.county,
        timeout=args.timeout,
        debug_path=debug_path,
    )
    if args.debug and not debug_path.exists():
        debug_path.write_text(html, encoding="utf-8")
    rows = parse_airperm_results(html)
    if not rows:
        logger.warning(
            "airperm returned 0 permits for the requested filter. "
            "TCEQ's Title V backend has been observed to return zero rows for "
            "every public query; supply --permits or --regulated-entity to "
            "drive Stage 2 directly."
        )
    else:
        logger.info(f"airperm enumerated {len(rows)} permits")
    return rows


def collect_permits_from_args(args: argparse.Namespace) -> List[Dict[str, str]]:
    permits: List[Dict[str, str]] = []
    if args.permits:
        for p in args.permits.split(","):
            p = p.strip()
            if p:
                permits.append({"permit_number": p, "regulated_entity": ""})
    if args.regulated_entity:
        permits.append({"permit_number": "", "regulated_entity": args.regulated_entity})
    return permits


def documents_for_permit(
    session: requests.Session,
    access_id: str,
    client_ip: str,
    permit: Dict[str, str],
    timeout: int,
    result_count: int = 50,
) -> List[Dict[str, str]]:
    """Search CFR for documents matching a permit. Try Primary ID first; fall
    back to Regulated Entity Name if no rows."""
    permit_no = (permit.get("permit_number") or "").strip()
    reg_ent = (permit.get("regulated_entity") or permit.get("co_name") or "").strip()
    queries: List[Dict] = []
    if permit_no:
        queries.append({"primary_id": permit_no})
    if reg_ent:
        queries.append({"reg_entity": reg_ent})
    if not queries:
        return []
    for q in queries:
        payload = cfr_search(
            session, access_id, client_ip,
            record_series=TITLEV_RECORD_SERIES, timeout=timeout,
            result_count=result_count, **q,
        )
        rows = cfr_extract_rows(payload)
        if rows:
            return rows
    return []


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download TCEQ Title V (Federal Operating Permit) documents.",
    )
    default_from, default_to = _default_dates()
    ap.add_argument("--from-date", type=parse_date_arg, default=default_from,
                    help=f"airperm start date (default: {default_from})")
    ap.add_argument("--to-date", type=parse_date_arg, default=default_to,
                    help=f"airperm end date (default: {default_to})")
    ap.add_argument("--status", choices=["ALL", "PENDING", "COMPLETE", "VOID"],
                    default="COMPLETE", help="airperm project status filter")
    ap.add_argument("--date-option",
                    choices=["date_received", "date_renewal", "date_issued",
                             "date_complete", "0"],
                    default="date_complete",
                    help="airperm date column (0 = no date filter)")
    ap.add_argument("--county", default="0",
                    help="airperm county code (0 = any). See tv.start dropdown.")
    ap.add_argument("--permits", default=None,
                    help="Comma-separated permit numbers; bypasses airperm enumeration.")
    ap.add_argument("--regulated-entity", default=None,
                    help="Single Regulated Entity Name to search CFR for; bypasses airperm.")
    ap.add_argument("--airperm-only", action="store_true",
                    help="Stage 1 only: build metadata index, skip CFR downloads.")
    ap.add_argument("--cfr-only", action="store_true",
                    help="Stage 2 only: requires --permits or --regulated-entity.")
    ap.add_argument("--scan-all", action="store_true",
                    help="Enumerate every document in xRecordSeries=1051 and "
                         "process them. Bypasses airperm/--permits/--regulated-entity.")
    ap.add_argument("--enumerate-only", action="store_true",
                    help="With --scan-all: write the metadata index but skip downloads.")
    ap.add_argument("--latest-only", action="store_true", default=True,
                    help="Keep only the latest dInDate per (xPrimaryID, dDocTitle). "
                         "On by default.")
    ap.add_argument("--no-latest-only", action="store_false", dest="latest_only",
                    help="Disable latest-only dedup; download every revision.")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    ap.add_argument("--max", type=int, default=None,
                    help="Cap number of permits processed (testing).")
    ap.add_argument("--max-docs-per-permit", type=int, default=None,
                    help="Cap docs downloaded per permit (testing).")
    ap.add_argument("--result-count", type=int, default=50,
                    help="CFR ResultCount per search (1-200).")
    ap.add_argument("--titles", nargs="+", default=list(ALLOWED_DOC_TITLES),
                    help=("dDocTitle whitelist (exact match). Pass --titles ALL "
                          "to disable filtering. Default: %(default)s"))
    ap.add_argument("--resume", action="store_true",
                    help="Skip permits whose PDFs already exist on disk.")
    ap.add_argument("--delay", type=float, default=0.75,
                    help="Sleep between requests (seconds).")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--debug", action="store_true",
                    help="Save raw airperm/CFR responses for inspection.")
    args = ap.parse_args()

    out_dir: Path = args.output_dir
    pdf_dir = out_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "tx_tceq_titlev_permits_index.csv"

    session = build_session(args.retries)

    # Stage 1: collect permits
    if args.cfr_only and not (args.permits or args.regulated_entity):
        logger.error("--cfr-only requires --permits or --regulated-entity")
        return 2

    # --scan-all: enumerate the entire xRecordSeries=1051 collection and
    # synthesize one "permit" entry per (post-filter) document. This bypasses
    # airperm and the per-permit CFR search.
    if args.scan_all:
        try:
            access_id, client_ip = cfr_get_access(session, args.timeout)
        except Exception as exc:
            logger.error(f"CFR access handshake failed: {exc}")
            return 1
        logger.info(f"CFR accessID={access_id} clientIP={client_ip}")

        page_size = max(1, min(args.result_count, 200))
        raw_docs = list(cfr_scan_all(
            session, access_id, client_ip,
            record_series=TITLEV_RECORD_SERIES,
            page_size=page_size,
            timeout=args.timeout,
            delay=args.delay,
        ))
        logger.info(f"scan-all: enumerated {len(raw_docs)} raw documents")

        if args.titles and args.titles != ["ALL"]:
            allow = set(args.titles)
            before = len(raw_docs)
            raw_docs = [d for d in raw_docs if (d.get("dDocTitle") or "").strip() in allow]
            logger.info(f"  title filter: {before} -> {len(raw_docs)}")
        if args.latest_only:
            before = len(raw_docs)
            raw_docs = latest_per_permit_title(raw_docs)
            logger.info(f"  latest-only dedup: {before} -> {len(raw_docs)}")

        permits = [
            {
                "permit_number": (d.get("xPrimaryID") or "").strip(),
                "regulated_entity": d.get("xRegEntName") or "",
                "_preselected_doc": d,
            }
            for d in raw_docs
        ]
        if args.max:
            permits = permits[: args.max]

        if args.enumerate_only:
            rows: List[Dict[str, str]] = []
            for p in permits:
                d = p["_preselected_doc"]
                rows.append({
                    "permit_number": p["permit_number"],
                    "regulated_entity": p["regulated_entity"],
                    "record_series": TITLEV_RECORD_SERIES,
                    "doc_name": d.get("dDocName", ""),
                    "doc_title": d.get("dDocTitle", ""),
                    "doc_in_date": d.get("dInDate", ""),
                    "document_url": (
                        f"{CFR_BASE}?IdcService=GET_FILE&dDocName={d.get('dDocName','')}"
                        f"&Rendition=Web&RevisionSelectionMethod=LatestReleased"
                    ),
                    "filename": cfr_doc_filename(p["permit_number"], d),
                    "status": "enumerated",
                })
            write_index(rows, index_path)
            logger.info(f"enumerate-only: wrote {len(rows)} rows to {index_path}")
            return 0

        # Skip airperm path entirely.
    elif args.permits or args.regulated_entity:
        permits = collect_permits_from_args(args)
        logger.info(f"Driving from CLI: {len(permits)} permit/entity entries")
    else:
        try:
            permits = collect_permits_from_airperm(session, args)
        except Exception as exc:
            logger.error(f"airperm enumeration failed: {exc}")
            permits = []

    if not args.scan_all and args.max:
        permits = permits[: args.max]

    if args.airperm_only:
        rows = [{**p, "status": "index_only"} for p in permits]
        write_index(rows, index_path)
        logger.info(f"airperm-only: wrote {len(rows)} rows to {index_path}")
        return 0

    if not permits:
        logger.warning("No permits to process. Supply --permits, --regulated-entity, or --scan-all.")
        return 0

    # Stage 2: CFR access handshake (skip if scan-all already did it)
    if not args.scan_all:
        try:
            access_id, client_ip = cfr_get_access(session, args.timeout)
        except Exception as exc:
            logger.error(f"CFR access handshake failed: {exc}")
            return 1
        logger.info(f"CFR accessID={access_id} clientIP={client_ip}")

    existing = read_existing_index(index_path) if args.resume else {}
    all_rows: List[Dict[str, str]] = []
    n_ok = n_skip = n_fail = n_empty = 0

    for i, p in enumerate(permits, 1):
        permit_no = (p.get("permit_number") or "").strip()
        preselected = p.get("_preselected_doc")
        try:
            if preselected is not None:
                docs = [preselected]
            else:
                docs = documents_for_permit(
                    session, access_id, client_ip, p, args.timeout,
                    result_count=args.result_count,
                )
                if args.titles and args.titles != ["ALL"]:
                    allow = set(args.titles)
                    before = len(docs)
                    docs = [d for d in docs if (d.get("dDocTitle") or "").strip() in allow]
                    if before and not docs:
                        logger.info(f"[{i}/{len(permits)}] {permit_no or p.get('regulated_entity','?')}: "
                                    f"all {before} docs filtered out by --titles")
                    elif before != len(docs):
                        logger.debug(f"filtered {before} -> {len(docs)} docs by --titles")
                if args.latest_only and len(docs) > 1:
                    before = len(docs)
                    docs = latest_per_permit_title(docs)
                    if before != len(docs):
                        logger.debug(f"latest-only: {before} -> {len(docs)} docs")
                if args.max_docs_per_permit:
                    docs = docs[: args.max_docs_per_permit]
        except Exception as exc:
            row = {**{k: v for k, v in p.items() if not k.startswith("_")},
                   "status": "error", "error": f"cfr_search: {exc}"}
            all_rows.append(row)
            n_fail += 1
            logger.error(f"[{i}/{len(permits)}] {permit_no or '?'} search: {exc}")
            time.sleep(args.delay)
            continue

        permit_meta = {k: v for k, v in p.items() if not k.startswith("_")}

        if not docs:
            row = {**permit_meta, "status": "no_documents", "record_series": TITLEV_RECORD_SERIES}
            all_rows.append(row)
            n_empty += 1
            logger.warning(f"[{i}/{len(permits)}] {permit_no or p.get('regulated_entity','?')}: 0 documents")
            time.sleep(args.delay)
            continue

        for d in docs:
            doc_name = d.get("dDocName") or ""
            # If we drove by regulated entity, fill permit_number from the
            # document's xPrimaryID so downstream rows are identifiable.
            doc_permit = permit_no or (d.get("xPrimaryID") or "").strip()
            row = {
                **permit_meta,
                "permit_number": doc_permit or permit_meta.get("permit_number", ""),
                "regulated_entity": permit_meta.get("regulated_entity") or d.get("xRegEntName") or "",
                "record_series": TITLEV_RECORD_SERIES,
                "doc_name": doc_name,
                "doc_title": d.get("dDocTitle") or "",
                "doc_in_date": d.get("dInDate") or "",
                "document_url": (
                    f"{CFR_BASE}?IdcService=GET_FILE&dDocName={doc_name}"
                    f"&Rendition=Web&RevisionSelectionMethod=LatestReleased"
                ),
            }
            fn = cfr_doc_filename(doc_permit, d)
            row["filename"] = fn
            dest = pdf_dir / fn

            existing_row = existing.get((permit_no, doc_name))
            if existing_row and existing_row.get("status") == "downloaded" and dest.exists():
                row["status"] = "skipped"
                row["size_bytes"] = existing_row.get("size_bytes", str(dest.stat().st_size))
                n_skip += 1
                all_rows.append(row)
                continue
            if dest.exists() and dest.stat().st_size > 0:
                row["status"] = "skipped"
                row["size_bytes"] = str(dest.stat().st_size)
                n_skip += 1
                all_rows.append(row)
                continue

            try:
                size = cfr_download(session, doc_name, dest, args.timeout)
                row["status"] = "downloaded"
                row["size_bytes"] = str(size)
                n_ok += 1
                logger.info(f"[{i}/{len(permits)}] {permit_no or p.get('regulated_entity','?')} -> {fn} ({size/1_048_576:.1f} MB)")
            except Exception as exc:
                row["status"] = "error"
                row["error"] = str(exc)
                n_fail += 1
                logger.error(f"[{i}/{len(permits)}] {fn}: {exc}")
            all_rows.append(row)
            if args.delay:
                time.sleep(args.delay)

        if i % 25 == 0:
            write_index(all_rows, index_path)

    write_index(all_rows, index_path)
    logger.info(
        f"Done. ok={n_ok} skipped={n_skip} no_documents={n_empty} failed={n_fail} "
        f"index={index_path}"
    )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
