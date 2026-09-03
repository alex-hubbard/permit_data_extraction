#!/usr/bin/env python3
"""Download Illinois EPA Bureau of Air permit documents for data centers.

Illinois EPA's permit records live in a DocuWare cabinet ("Air Documents")
fronted by the IEPA Document Explorer.  Data-center permits there are mostly
minor-source construction permits and FESOPs that never went through public
notice, so they are absent from the BOA public-notice archive already pulled
by ``download_il_epa_boa_archive.py``.  This script reaches the underlying
cabinet directly and searches it by site name, and complements that with the
Document Explorer's address search (shell-company permittees) and the BOA
public-notice API (draft permits / statements of basis for the few FESOPs
that were noticed).

Portal mechanics
----------------
1. IEPA Document Explorer (ASP.NET MVC, plain GET, HTML fragments):
     GET https://webapps.illinois.gov/EPA/DocumentExplorer/Attributes/Search
         ?Name=&Address=&City=&Zip=&County=&InterestId=&Page=N   (10 rows/page)
     GET https://webapps.illinois.gov/EPA/DocumentExplorer/Documents/Index/<IEPAID>
         -> facility header + one row per document category, each linking to
            https://docuware7.illinois.gov/DocuWare/PlatformRO/WebClient/2/Integration?ep=<enc>
2. The Integration URL 302-redirects to the DocuWare result list with
   ``q=[IEPAID]="<id>" AND [CATEGORY]="03K"`` and a ``_auth`` token.  The
   read-only Platform REST API accepts that token when it is appended to the
   request URL together with ``_cs`` (DocuWare's JS string hash of the
   relative URL) and echoed in an ``X-DW-AUTH`` header.  The token is not
   bound to the original query, so the cabinet can be searched with any DWQL
   condition:
     POST /DocuWare/PlatformRO/FileCabinets/<fc>/Query/DialogExpression
          ?dialogId=00000000-...&fields=BUREAUID,SITENAME,...&_auth=..&_cs=..
          {"CompleteCondition": "[SITENAME] LIKE \\"*MICROSOFT*\\"", "Start":0, "Count":200, ...}
     GET  /DocuWare/PlatformRO/FileCabinets/<fc>/Documents/<DWDOCID>/FileDownload
          ?targetFileType=PDF&keepAnnotations=false&_auth=..&_cs=..
   Cabinet fields: BUREAUID (IEPA air ID, e.g. 031449ADS), SITENAME, ITEMDATE,
   PERMITID, CATEGORY (03K = "Air Permit - Final", 08 = "Compliance"; other
   codes such as 03L/03M/03N/10G/20 exist in the cabinet but the agency's
   public UI does not expose them, so they are indexed but not downloaded
   unless requested with --categories).
3. BOA public-notice archive (WebSiteApi, see download_il_epa_boa_archive.py):
     POST .../PublicNotices/GetArchivedAirPublicNotices/  (all permit types)
     GET  .../PublicNotices/GetPermitDocumentsList/?facilityId=&permitId=
     GET  .../PublicNotices/GetAirPermitDocument/<PublicNoticeId>
   Documents whose PublicNoticeId is already in the BOA archive index are
   flagged ``skipped_in_archive`` instead of re-downloaded.

Output layout
-------------
  <RAW_DATA_DIR>/il_epa_dc_permits/
    pdfs/il_dw_<BUREAUID>_<PERMITID>_<DWDOCID>_<category>.pdf
    pdfs/il_<FacilityId>_<PermitId>_<PublicNoticeId>_<type>.pdf   (BOA lane)
    il_epa_dc_permits_index.csv          one row per document (appended as we go)
    il_epa_dc_facilities.csv             facility-level summary
    il_epa_dc_address_candidates.csv     address-lane hits not matched by name
    il_epa_dc_permits.log
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

import dc_targets  # noqa: E402

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
OUTPUT_DIR = RAW_DATA_DIR / "il_epa_dc_permits"
BOA_ARCHIVE_DIR = RAW_DATA_DIR / "il_epa_boa_archive"
BOA_ARCHIVE_INDEX = BOA_ARCHIVE_DIR / "il_epa_boa_archive_index.csv"

DE_BASE = "https://webapps.illinois.gov/EPA/DocumentExplorer"
DW_HOST = "https://docuware7.illinois.gov"
DW_PLATFORM = "/DocuWare/PlatformRO"
DW_CABINET = "87263d25-3843-4ee0-907b-464feabd20f0"  # "Air Documents"
DW_DIALOG = "00000000-0000-0000-0000-000000000000"
DW_FIELDS = "BUREAUID,SITENAME,ITEMDATE,PERMITID,PERMITLOG,COMMENTTEXT,CATEGORY,IEPAID"
DW_PAGE = 200
TOKEN_SEED_IEPAID = "170002499384"  # Microsoft Hoffman Estates (CHI05) – any facility works
TOKEN_TTL_SECONDS = 10 * 60  # integration tokens were observed to die somewhere between 13 and 16 minutes

BOA_API = "https://webapps.illinois.gov/EPA/WebSiteApi/api/PublicNotices/"
BOA_PORTAL = "https://epa.illinois.gov/public-notices/boa-notices/archive.html"

CATEGORY_LABELS = {
    "03K": "Air Permit - Final",
    "08": "Compliance",
    "13": "NPDES Permit",
    "21A": "Leaking UST Technical",
}
DEFAULT_CATEGORIES = ["03K"]

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

DC_SIC_CODES = {"7374", "7375", "7376", "7379"}
DC_NAME_RE = re.compile(r"data ?cent|datacenter|colocation|colo\b|server farm|hyperscale", re.I)
# Address-lane hits that share a street address with a data center but are plainly not one.
NOT_DC_NAME_RE = re.compile(
    r"demolition|wrecking|excavat|asbestos|abatement|landscap|school|township|county hwy|\bbank\b|church|"
    r"pws\b|water dept|ready mix|sand & gravel|substation", re.I)

# Curated Illinois search terms (DocuWare SITENAME wildcard, case-insensitive).
# These are the names IEPA actually files data-center permits under.
CURATED_TERMS = [
    "DATA CENTER", "DATACENTER", "DATA CENTRE", "COLOCATION", "HYPERSCALE",
    "MICROSOFT", "META PLATFORMS", "FACEBOOK", "GOOGLE", "STREAM DATA",
    "DIGITAL REALTY", "DIGITAL GRAND", "CYRUSONE", "COMPASS DATA", "POWERHOUSE DATA",
    "PRIME DATA", "T5@", "T5 DATA", "QUALITY TECHNOLOGY", "QTS REALTY", "VANTAGE DATA",
    "EQUINIX", "DATABANK", "ALIGNED", "SABEY", "ELEMENT CRITICAL",
    "EDGECONNEX", "EDGE CONNEX", "ENSONO", "CLOUDHQ", "STACK INFRA", "CORESITE",
    "NTT GLOBAL", "SDC CHI", "SI CHIO", "BUSSE ROAD LLC", "ELK GROVE DATA",
    "MERIDIAN PARKWAY", "TIERPOINT", "ZCOLO", "CENTURYLINK TECH", "FORSYTHE",
    "NEW CONTINUUM", "EDGED CHICAGO", "DIPPER VENTURES", "CRG WEST", "725 S WELLS",
    "SERVERFARM", "SKYBOX", "NETRALITY", "EVOQUE", "FLEXENTIAL", "COLOGIX",
    "CHIRISA", "KARIS", "RACKHOUSE", "GIGENET", "SINGLEHOP", "FDC SERVERS",
    "METRO EDGE", "DATA CANOPY", "CENTERSQUARE", "VADATA", "AMAZON DATA",
    "ORACLE", "APPLE INC", "CRUSOE", "COREWEAVE", "TRACT CAPITAL", "ROWAN DIGITAL",
    "CORSCALE", "YONDR", "ADA INFRA", "APPLIED DIGITAL", "HUT 8", "CORE SCIENTIFIC",
]

# Telecom / logistics / records companies: prefix-match hundreds of switch
# sites, cell towers and warehouses.  Their sites only qualify when the site
# name itself says "data center" (caught by the generic terms above).
EXCLUDED_TERM_RE = re.compile(
    r"^(verizon|at&t|lumen|zayo|cogent|sba communications|consolidated communications|"
    r"iron mountain|amazon$|amazon\.com|switch|design llc|ntt$|mara|tract$|meta$|apple$|"
    r"capri investment|centerpoint|cloverleaf|green door|mapletree|menlo equities|"
    r"oppidan|starpoint|the daly|tmbg|gi partners|legacy investing|a5 corporation|360 tcs|ascent|design$)",
    re.I,
)
CORP_SUFFIX_RE = re.compile(
    r"\b(inc|llc|l\.?l\.?c|corp|corporation|company|co|lp|l\.?p|ltd|trust|holdings|"
    r"realty|group|management|preparatory)\b\.?", re.I,
)

INDEX_FIELDS = [
    "source", "doc_id", "bureau_id", "iepaid", "site_name", "permit_id", "item_date",
    "category_code", "category_label", "pages", "boa_permit_type", "boa_submittal_type",
    "boa_doc_type", "matched_terms", "document_url", "local_path", "status", "note",
]
FACILITY_FIELDS = [
    "bureau_id", "iepaid", "site_name", "address", "city", "matched_terms",
    "n_docs_total", "n_docs_final_permit", "categories", "in_boa_archive",
]
CANDIDATE_FIELDS = ["iepaid", "name", "address", "city", "zip", "lbl_address", "lbl_company", "lbl_city"]


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def make_session() -> requests.Session:
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[408, 429, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/javascript, */*; q=0.01",
                      "Accept-Language": "en-US,en;q=0.9"})
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def js_hash(s: str) -> int:
    """DocuWare WebClient's DW.Utils.calculateChecksum (Java-style 32-bit string hash)."""
    h = 0
    for ch in s:
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
    return h - 0x100000000 if h >= 0x80000000 else h


def dw_date(v) -> str:
    """'/Date(1646784000000)/' -> '2022-03-09'."""
    if not v:
        return ""
    m = re.search(r"-?\d+", str(v))
    if not m:
        return str(v)
    try:
        return (dt.datetime(1970, 1, 1) + dt.timedelta(milliseconds=int(m.group(0)))).strftime("%Y-%m-%d")
    except (OverflowError, ValueError):
        return ""


def safe_stem(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", s).strip("_")[:120]


# --------------------------------------------------------------------------
# Document Explorer
# --------------------------------------------------------------------------
DE_ROW_RE = re.compile(
    r'<td><a href="/EPA/DocumentExplorer/Documents/Index/(\d+)"[^>]*>([^<]+)</a></td>\s*'
    r"<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>"
)
DE_CAT_RE = re.compile(
    r'<td><a class="dw-modalLink"[^>]*href="([^"]+)">([^<]+)</a></td>\s*'
    r"<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>"
)
DE_HEADER_RE = re.compile(r"<strong>([^<]+?)\s*&ndash;\s*(\d+)</strong><br />\s*([^<]*)<br />\s*([^<]*)")


def unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
            .replace("&lt;", "<").replace("&gt;", ">").strip())


class DocumentExplorer:
    def __init__(self, session: requests.Session, delay: float, timeout: int):
        self.s, self.delay, self.timeout = session, delay, timeout

    def search(self, *, name: str = "", address: str = "", city: str = "", max_pages: int = 30) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        page = 1
        while page <= max_pages:
            params = {"Name": name, "Address": address, "City": city, "Zip": "", "County": "",
                      "InterestId": "", "ActiveTab": "#attributesTab", "SearchButton": "Search", "Page": str(page)}
            r = self.s.get(f"{DE_BASE}/Attributes/Search", params=params,
                           headers={"X-Requested-With": "XMLHttpRequest", "Accept": "text/html"}, timeout=self.timeout)
            r.raise_for_status()
            html = r.text
            found = DE_ROW_RE.findall(html)
            for iepaid, nm, addr, cty, st, zp in found:
                rows.append({"iepaid": iepaid, "name": unescape(nm), "address": unescape(addr),
                             "city": unescape(cty), "state": st.strip(), "zip": zp.strip()})
            time.sleep(self.delay)
            if f"Page={page + 1}&" not in html.replace("&amp;", "&"):
                break
            page += 1
        return rows

    def facility(self, iepaid: str) -> Dict[str, object]:
        r = self.s.get(f"{DE_BASE}/Documents/Index/{iepaid}", headers={"Accept": "text/html"}, timeout=self.timeout)
        r.raise_for_status()
        html = r.text
        out: Dict[str, object] = {"iepaid": iepaid, "name": "", "address": "", "city_line": "", "categories": []}
        m = DE_HEADER_RE.search(html)
        if m:
            out["name"] = unescape(m.group(1))
            out["address"] = unescape(m.group(3))
            out["city_line"] = unescape(re.sub(r"\s+", " ", m.group(4)))
        cats = []
        for ep, label, cnt, pages, bureau in DE_CAT_RE.findall(html):
            cats.append({"ep": ep.replace("&amp;", "&"), "label": unescape(label), "count": cnt.strip(),
                         "pages": pages.strip(), "bureau": unescape(bureau)})
        out["categories"] = cats
        time.sleep(self.delay)
        return out


# --------------------------------------------------------------------------
# DocuWare Platform (read-only) client
# --------------------------------------------------------------------------
class DocuWare:
    def __init__(self, session: requests.Session, explorer: DocumentExplorer, delay: float, timeout: int):
        self.s, self.de, self.delay, self.timeout = session, explorer, delay, timeout
        self.auth: Optional[str] = None
        self.token_time = 0.0

    # -- auth -------------------------------------------------------------
    def token_from_ep(self, ep_url: str) -> str:
        r = self.s.get(ep_url, allow_redirects=False, timeout=self.timeout)
        loc = r.headers.get("Location", "")
        qs = parse_qs(urlparse(loc).query)
        if "_auth" not in qs:
            raise RuntimeError(f"Integration URL did not yield _auth (HTTP {r.status_code})")
        return qs["_auth"][0]

    def refresh_token(self) -> None:
        fac = self.de.facility(TOKEN_SEED_IEPAID)
        cats = fac["categories"]  # type: ignore[index]
        if not cats:
            raise RuntimeError("Seed facility page has no DocuWare category links")
        self.auth = self.token_from_ep(cats[0]["ep"])  # type: ignore[index]
        self.token_time = time.time()
        logger.debug("DocuWare token refreshed")

    def _signed(self, rel: str) -> str:
        if self.auth is None or time.time() - self.token_time > TOKEN_TTL_SECONDS:
            self.refresh_token()
        rel = rel + ("&" if "?" in rel else "?") + "_auth=" + self.auth  # type: ignore[operator]
        return f"{DW_HOST}{rel}&_cs={js_hash(rel)}"

    def _request(self, method: str, rel: str, **kw) -> requests.Response:
        headers_in = dict(kw.pop("headers", {}) or {})
        timeout = kw.pop("timeout", self.timeout)
        last_exc: Optional[Exception] = None
        for attempt in (1, 2, 3):
            url = self._signed(rel)
            headers = dict(headers_in)
            headers["X-DW-AUTH"] = self.auth
            try:
                r = self.s.request(method, url, headers=headers, timeout=timeout, **kw)
            except requests.RequestException as e:
                last_exc = e
                logger.warning(f"DocuWare transport error (attempt {attempt}): {e.__class__.__name__}")
                time.sleep(3 * attempt)
                self.refresh_token()
                continue
            if r.status_code in (401, 403, 500) and attempt < 3:
                logger.warning(f"DocuWare HTTP {r.status_code} (attempt {attempt}); refreshing token")
                time.sleep(2)
                self.refresh_token()
                continue
            return r
        raise RuntimeError(f"DocuWare request failed after retries: {last_exc}")

    # -- queries ----------------------------------------------------------
    def query(self, condition: str, fields: str = DW_FIELDS) -> List[Dict[str, object]]:
        rel = (f"{DW_PLATFORM}/FileCabinets/{DW_CABINET}/Query/DialogExpression"
               f"?dialogId={DW_DIALOG}&fields={quote(fields)}")
        out: List[Dict[str, object]] = []
        start = 0
        while True:
            body = {"CompleteCondition": condition, "CompleteConditionInInvariantCulture": False,
                    "Condition": [], "SortOrder": [{"Field": "ITEMDATE", "Direction": "Asc"}],
                    "ForceRefresh": True, "Operation": "And", "AdditionalResultFields": [],
                    "Start": start, "Count": DW_PAGE, "IncludeSuggestions": False}
            r = self._request("POST", rel, json=body)
            if r.status_code != 200:
                raise RuntimeError(f"DocuWare query failed HTTP {r.status_code}: {r.text[:200]}")
            payload = r.json()
            items = payload.get("Items") or []
            for it in items:
                d = {f["FieldName"]: f.get("Item") for f in it.get("Fields", [])}
                out.append({
                    "doc_id": str(d.get("DWDOCID") or it.get("Id") or ""),
                    "bureau_id": (d.get("BUREAUID") or "").strip(),
                    "site_name": (d.get("SITENAME") or "").strip(),
                    "item_date": dw_date(d.get("ITEMDATE")),
                    "permit_id": (d.get("PERMITID") or "").strip(),
                    "permit_log": (d.get("PERMITLOG") or "").strip(),
                    "comment": (d.get("COMMENTTEXT") or "").strip(),
                    "category_code": (d.get("CATEGORY") or "").strip(),
                    "iepaid": str(d.get("IEPAID") or "").strip(),
                    "pages": it.get("TotalPages") or d.get("DWPAGECOUNT") or "",
                })
            time.sleep(self.delay)
            count = payload.get("Count") or {}
            if len(items) < DW_PAGE or not count.get("HasMore", False) and len(out) >= int(count.get("Value") or 0):
                break
            start += DW_PAGE
            if start > 20000:  # safety
                break
        return out

    def download(self, doc_id: str, dest: Path) -> Tuple[bool, str]:
        rel = (f"{DW_PLATFORM}/FileCabinets/{DW_CABINET}/Documents/{doc_id}/FileDownload"
               f"?targetFileType=PDF&keepAnnotations=false")
        try:
            r = self._request("GET", rel, headers={"Accept": "*/*"}, timeout=self.timeout * 3, stream=True)
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:200]
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        first = b""
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    if not first:
                        first = chunk[:8]
                    f.write(chunk)
        if not first.startswith(b"%PDF"):
            tmp.unlink(missing_ok=True)
            return False, f"not a PDF ({r.headers.get('Content-Type')})"
        tmp.replace(dest)
        time.sleep(self.delay)
        return True, ""


# --------------------------------------------------------------------------
# BOA public-notice archive API
# --------------------------------------------------------------------------
class BoaArchive:
    def __init__(self, session: requests.Session, delay: float, timeout: int):
        self.s, self.delay, self.timeout = session, delay, timeout
        self.h = {"Origin": "https://epa.illinois.gov", "Referer": BOA_PORTAL}

    def list_all(self) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        start, size = 0, 500
        while True:
            body = {"draw": "1", "start": str(start), "length": str(size), "facilityName": "", "airIdNumber": "",
                    "permitId": "", "city": "", "permitType": "", "publicNoticeDateRangeStart": "",
                    "publicNoticeDateRangeEnd": "", "usepaNoticeDateRangeStart": "", "usepaNoticeDateRangeEnd": "",
                    "county": "", "sicCode": "", "order[0][column]": "6", "order[0][dir]": "desc"}
            r = self.s.post(BOA_API + "GetArchivedAirPublicNotices/", data=body, headers=self.h, timeout=self.timeout)
            r.raise_for_status()
            batch = r.json().get("data") or []
            rows.extend(batch)
            time.sleep(self.delay)
            if len(batch) < size:
                break
            start += size
        return rows

    def documents(self, facility_id: str, permit_id: str) -> List[Dict[str, object]]:
        params = {"facilityId": facility_id, "permitId": permit_id, "draw": "1", "start": "0", "length": "500",
                  "order[0][column]": "3", "order[0][dir]": "desc"}
        r = self.s.get(BOA_API + "GetPermitDocumentsList/", params=params, headers=self.h, timeout=self.timeout)
        r.raise_for_status()
        time.sleep(self.delay)
        return r.json().get("data") or []

    def download(self, public_notice_id: str, dest: Path) -> Tuple[bool, str]:
        try:
            r = self.s.get(f"{BOA_API}GetAirPermitDocument/{public_notice_id}", headers=self.h,
                           timeout=self.timeout * 3, stream=True)
        except requests.RequestException as e:
            return False, str(e)[:200]
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        first = b""
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    if not first:
                        first = chunk[:8]
                    f.write(chunk)
        if not first.startswith(b"%PDF"):
            tmp.unlink(missing_ok=True)
            return False, f"not a PDF ({r.headers.get('Content-Type')})"
        tmp.replace(dest)
        time.sleep(self.delay)
        return True, ""


# --------------------------------------------------------------------------
# Term building
# --------------------------------------------------------------------------
def clean_company(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 &@+-]", " ", name)
    s = CORP_SUFFIX_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_terms(extra: Iterable[str] = ()) -> List[str]:
    terms: List[str] = list(CURATED_TERMS)
    for t in dc_targets.entity_terms("IL"):
        terms.append(clean_company(t))
    try:
        df = dc_targets.load_combined()
        sub = df[df["state"].astype(str).str.upper() == "IL"]
        for col in ("company", "operator"):
            if col in sub.columns:
                terms.extend(clean_company(v) for v in sub[col].dropna().unique() if isinstance(v, str))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not load LBL facility list for terms: {e}")
    terms.extend(extra)
    curated = {t.upper() for t in CURATED_TERMS} | {t.upper() for t in extra}
    seen: Set[str] = set()
    out: List[str] = []
    for t in terms:
        t = re.sub(r"[\"*?]", "", t).strip()
        key = t.upper()
        if not key or key in seen:
            continue
        if key not in curated:
            # dc_targets / LBL company names: drop short, generic or excluded prefixes
            if len(key) < 5 or key.lower() in dc_targets.STOPWORDS or EXCLUDED_TERM_RE.search(key):
                continue
        seen.add(key)
        out.append(key)
    return out


STREET_SUFFIXES = {"road", "rd", "street", "st", "avenue", "ave", "blvd", "boulevard", "drive", "dr", "lane", "ln",
                   "terrace", "ter", "pkwy", "parkway", "way", "ct", "court", "hwy", "highway", "pl", "place", "cir"}
DIRECTIONALS = {"n", "s", "e", "w", "north", "south", "east", "west"}


def lbl_addresses() -> List[Dict[str, str]]:
    """Street-name probes (plus house numbers for filtering) for every LBL-map facility in Illinois."""
    try:
        df = dc_targets.load_combined()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"LBL list unavailable: {e}")
        return []
    sub = df[df["state"].astype(str).str.upper() == "IL"]
    probes: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for _, row in sub.iterrows():
        raw = str(row.get("address") or "").strip()
        city = str(row.get("city") or "").strip()
        if not raw or raw.lower() == "nan":
            continue
        addr = raw.split(",")[0]
        addr = re.sub(r"\b(bldg|building|suite|ste)\b.*$", "", addr, flags=re.I).strip()
        numbers = re.findall(r"\b\d{1,6}\b", addr) if not re.search(r"\bblock\b", addr, re.I) else []
        words = [w.strip(".") for w in re.split(r"[\s/&]+", addr) if w.strip(".")]
        street: List[str] = []
        for w in words:
            wl = w.lower()
            if wl.isdigit() or wl in DIRECTIONALS or wl in ("block", "surrounding", "and", "of", "the"):
                continue
            if wl in STREET_SUFFIXES:
                if street:
                    break
                continue
            street.append(w)
            if len(street) == 2 or (len(street) == 1 and len(w) > 3):
                break
        if not street:
            continue
        probe = " ".join(street)
        key = (probe.lower(), city.lower())
        if key in seen:
            continue
        seen.add(key)
        probes.append({"probe": probe, "numbers": ",".join(numbers), "lbl_address": raw,
                       "lbl_city": city, "lbl_company": str(row.get("company") or row.get("operator") or "")})
    return probes


def city_key(c: str) -> str:
    c = re.sub(r"[^a-z ]", "", (c or "").lower())
    return c.split()[0] if c.split() else ""


# --------------------------------------------------------------------------
# Index helpers
# --------------------------------------------------------------------------
class IndexWriter:
    def __init__(self, path: Path, fields: List[str]):
        self.path, self.fields = path, fields
        self.rows: List[Dict[str, str]] = []
        if path.exists():
            with path.open(newline="", encoding="utf-8") as f:
                self.rows = list(csv.DictReader(f))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=fields).writeheader()

    def done_keys(self) -> Dict[Tuple[str, str], str]:
        out: Dict[Tuple[str, str], str] = {}
        for r in self.rows:
            out[(r.get("source", ""), r.get("doc_id", ""))] = r.get("status", "")
        return out

    def append(self, row: Dict[str, object]) -> None:
        clean = {k: ("" if row.get(k) is None else str(row.get(k))) for k in self.fields}
        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.fields).writerow(clean)
        self.rows.append(clean)


def load_boa_archive_ids() -> Tuple[Set[str], Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    """PublicNoticeIds present in the BOA archive, plus (facility, permit) pairs (all / with Final Permit)."""
    ids: Set[str] = set()
    pairs: Set[Tuple[str, str]] = set()
    final_pairs: Set[Tuple[str, str]] = set()
    if BOA_ARCHIVE_INDEX.exists():
        with BOA_ARCHIVE_INDEX.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ids.add(str(r.get("public_notice_id", "")).strip())
                pair = (str(r.get("facility_id", "")).strip(), str(r.get("permit_id", "")).strip())
                pairs.add(pair)
                if "final permit" in str(r.get("public_notice_type", "")).lower():
                    final_pairs.add(pair)
    # PDFs still on disk under the archive directory (names il_<fid>_<pid>_<pnid>_...)
    if BOA_ARCHIVE_DIR.exists():
        for p in BOA_ARCHIVE_DIR.glob("il_*_*_*_*.pdf"):
            parts = p.name.split("_")
            if len(parts) >= 4:
                ids.add(parts[3])
                pairs.add((parts[1], parts[2]))
    return ids, pairs, final_pairs


# --------------------------------------------------------------------------
# Main crawl
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="Stop after downloading this many new PDFs (tests)")
    ap.add_argument("--max-pdfs", type=int, default=2000, help="Hard cap on PDFs downloaded in this run")
    ap.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES),
                    help="DocuWare CATEGORY codes to download (default 03K = Air Permit - Final; "
                         "08 = Compliance; other codes are not exposed by the agency UI)")
    ap.add_argument("--terms", default="", help="Comma-separated extra SITENAME search terms")
    ap.add_argument("--only-terms", action="store_true", help="Use only --terms (skip curated/dc_targets/LBL terms)")
    ap.add_argument("--skip-address-lane", action="store_true", help="Skip Document Explorer address probes")
    ap.add_argument("--skip-boa-lane", action="store_true", help="Skip the BOA public-notice archive lane")
    ap.add_argument("--no-download", action="store_true", help="Index only")
    ap.add_argument("--redownload-archived", action="store_true",
                    help="Download final permits even when the BOA archive already holds one for the permit")
    ap.add_argument("--delay", type=float, default=0.4, help="Seconds between requests")
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = ap.parse_args()

    out_dir: Path = args.output_dir.expanduser().resolve()
    pdf_dir = out_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    logger.add(out_dir / "il_epa_dc_permits.log", level="DEBUG", rotation="20 MB", enqueue=True)

    wanted_categories = {c.strip().upper() for c in args.categories.split(",") if c.strip()}
    index = IndexWriter(out_dir / "il_epa_dc_permits_index.csv", INDEX_FIELDS)
    done = index.done_keys()
    logger.info(f"Existing index rows: {len(index.rows)}")

    archive_ids, archive_pairs, archive_final_pairs = load_boa_archive_ids()
    logger.info(f"BOA archive: {len(archive_ids)} document ids, {len(archive_pairs)} facility+permit pairs")

    session = make_session()
    explorer = DocumentExplorer(session, args.delay, args.timeout)
    dw = DocuWare(session, explorer, args.delay, args.timeout)
    boa = BoaArchive(session, args.delay, args.timeout)

    downloaded = 0
    failed = 0
    skipped_archive = 0
    listed = 0
    budget = args.max_pdfs if args.limit is None else min(args.limit, args.max_pdfs)

    # ---- facility registry --------------------------------------------------
    facilities: Dict[str, Dict[str, object]] = {}  # bureau_id -> info

    def touch_facility(bureau_id: str, site_name: str, iepaid: str, term: str) -> None:
        f = facilities.setdefault(bureau_id, {"bureau_id": bureau_id, "iepaid": "", "site_name": "",
                                              "address": "", "city": "", "matched_terms": set(),
                                              "docs": {}, "in_boa_archive": ""})
        if site_name and (not f["site_name"] or len(site_name) > len(f["site_name"])):  # type: ignore[arg-type]
            f["site_name"] = site_name
        if iepaid and not f["iepaid"]:
            f["iepaid"] = iepaid
        f["matched_terms"].add(term)  # type: ignore[union-attr]

    def register_docs(docs: List[Dict[str, object]], term: str) -> int:
        fresh = 0
        for d in docs:
            bid = d["bureau_id"] or f"iepaid:{d['iepaid']}"
            touch_facility(str(bid), str(d["site_name"]), str(d["iepaid"]), term)
            store = facilities[str(bid)]["docs"]  # type: ignore[index]
            if d["doc_id"] not in store:  # type: ignore[operator]
                store[d["doc_id"]] = d  # type: ignore[index]
                fresh += 1
        return fresh

    # ---- Lane A: DocuWare SITENAME wildcard search ---------------------------
    extra = [t.strip() for t in args.terms.split(",") if t.strip()]
    terms = [t.upper() for t in extra] if args.only_terms else build_terms(extra)
    logger.info(f"Lane A: {len(terms)} SITENAME search terms")
    for i, term in enumerate(terms, 1):
        cond = f'[SITENAME] LIKE "*{term}*"'
        try:
            docs = dw.query(cond)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{i}/{len(terms)}] {term!r}: query failed: {e}")
            continue
        fresh = register_docs(docs, f"name:{term}")
        if docs:
            sites = {d["bureau_id"] for d in docs}
            logger.info(f"[{i}/{len(terms)}] {term!r}: {len(docs)} docs / {len(sites)} sites ({fresh} new docs)")

    # ---- Lane B: Document Explorer address probes -> IEPAID queries ---------
    candidates = IndexWriter(out_dir / "il_epa_dc_address_candidates.csv", CANDIDATE_FIELDS)
    cand_seen = {r["iepaid"] for r in candidates.rows}
    if not args.skip_address_lane:
        probes = lbl_addresses()
        logger.info(f"Lane B: {len(probes)} LBL address probes")
        known_iepaids = {str(f["iepaid"]) for f in facilities.values() if f["iepaid"]}
        for i, pr in enumerate(probes, 1):
            city_word = pr["lbl_city"].split()[0] if pr["lbl_city"].split() else ""
            try:
                rows = explorer.search(address=pr["probe"], city=city_word, max_pages=10)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{i}/{len(probes)}] address {pr['probe']!r}: {e}")
                continue
            numbers = {n for n in pr["numbers"].split(",") if n}
            for row in rows:
                if city_key(pr["lbl_city"]) and city_key(row["city"]) != city_key(pr["lbl_city"]):
                    continue
                if row["iepaid"] in known_iepaids:
                    continue
                if NOT_DC_NAME_RE.search(row["name"]) and not DC_NAME_RE.search(row["name"]):
                    continue
                addr_tokens = set(re.findall(r"\d+", row["address"]))
                number_ok = bool(numbers & addr_tokens)
                name_ok = bool(DC_NAME_RE.search(row["name"])) or any(
                    t in row["name"].upper() for t in terms if len(t) >= 6)
                shell_ok = bool(re.search(r"\b(LLC|LP|L\.P\.|Owner|PropCo|Holdings|Ventures|Project|Development|Realty|Properties|Partners)\b",
                                          row["name"], re.I))
                if numbers and not number_ok:
                    continue  # numbered LBL address, different house number
                if not (number_ok or name_ok or shell_ok):
                    if row["iepaid"] not in cand_seen:
                        candidates.append({**{k: row.get(k, "") for k in ("iepaid", "name", "address", "city", "zip")},
                                           "lbl_address": pr["lbl_address"], "lbl_company": pr["lbl_company"],
                                           "lbl_city": pr["lbl_city"]})
                        cand_seen.add(row["iepaid"])
                    continue
                try:
                    docs = dw.query(f'[IEPAID]="{row["iepaid"]}"')
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"IEPAID {row['iepaid']} query failed: {e}")
                    continue
                known_iepaids.add(row["iepaid"])
                fresh = register_docs(docs, f"address:{pr['probe']}")
                for d in docs:
                    bid = d["bureau_id"] or f"iepaid:{d['iepaid']}"
                    fac = facilities.get(str(bid))
                    if fac is not None:
                        fac["address"] = fac["address"] or row["address"]
                        fac["city"] = fac["city"] or row["city"]
                logger.info(f"[{i}/{len(probes)}] {pr['probe']!r} -> {row['name']!r} ({row['address']}, {row['city']}): "
                            f"{len(docs)} docs ({fresh} new)")
                if not docs and row["iepaid"] not in cand_seen:
                    candidates.append({**{k: row.get(k, "") for k in ("iepaid", "name", "address", "city", "zip")},
                                       "lbl_address": pr["lbl_address"], "lbl_company": pr["lbl_company"],
                                       "lbl_city": pr["lbl_city"]})
                    cand_seen.add(row["iepaid"])

    # ---- Lane C: BOA public-notice archive ------------------------------------
    boa_rows: List[Dict[str, object]] = []
    if not args.skip_boa_lane:
        try:
            boa_all = boa.list_all()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"BOA archive listing failed: {e}")
            boa_all = []
        logger.info(f"Lane C: {len(boa_all)} public-notice rows in BOA archive")
        term_set = [t for t in terms if len(t) >= 6]
        for r in boa_all:
            fid = str(r.get("FacilityId") or "").strip()
            name = str(r.get("FacilityName") or "").strip()
            sic = str(r.get("SicCode") or "").strip()
            up = name.upper()
            reason = ""
            if fid in facilities:
                reason = "bureau_id"
            elif sic in DC_SIC_CODES:
                reason = f"sic:{sic}"
            elif DC_NAME_RE.search(name):
                reason = "name_pattern"
            else:
                hit = next((t for t in term_set if t in up), None)
                if hit:
                    reason = f"name:{hit}"
            if reason:
                r["_reason"] = reason
                boa_rows.append(r)
        pairs = {}
        for r in boa_rows:
            key = (str(r["FacilityId"]).strip(), str(r["PermitId"]).strip())
            pairs.setdefault(key, r)
        logger.info(f"Lane C: {len(boa_rows)} DC rows -> {len(pairs)} facility+permit pairs")
        for (fid, pid), r in pairs.items():
            touch_facility(fid, str(r.get("FacilityName") or ""), "", f"boa:{r['_reason']}")
            facilities[fid]["city"] = facilities[fid]["city"] or str(r.get("City") or "")
            # pull final permits for BOA-only facilities from DocuWare too
            if not facilities[fid]["docs"]:
                try:
                    register_docs(dw.query(f'[BUREAUID]="{fid}"'), f"boa:{r['_reason']}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"BUREAUID {fid} query failed: {e}")

    # ---- Facility summary -----------------------------------------------------
    for bid, f in facilities.items():
        docs = f["docs"]  # type: ignore[assignment]
        f["in_boa_archive"] = "1" if any((bid, str(d["permit_id"])) in archive_pairs for d in docs.values()) else ""  # type: ignore[union-attr]
        if f["iepaid"] and not f["address"]:
            try:
                info = explorer.facility(str(f["iepaid"]))
                f["address"] = info["address"]
                f["city"] = info["city_line"]
            except Exception as e:  # noqa: BLE001
                logger.debug(f"facility page {f['iepaid']}: {e}")
    fac_path = out_dir / "il_epa_dc_facilities.csv"
    with fac_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FACILITY_FIELDS)
        w.writeheader()
        for bid, f in sorted(facilities.items()):
            docs = list(f["docs"].values())  # type: ignore[union-attr]
            cats = {}
            for d in docs:
                cats[d["category_code"]] = cats.get(d["category_code"], 0) + 1
            w.writerow({
                "bureau_id": bid, "iepaid": f["iepaid"], "site_name": f["site_name"], "address": f["address"],
                "city": f["city"], "matched_terms": "; ".join(sorted(f["matched_terms"])),  # type: ignore[arg-type]
                "n_docs_total": len(docs), "n_docs_final_permit": cats.get("03K", 0),
                "categories": json.dumps(cats, sort_keys=True), "in_boa_archive": f["in_boa_archive"],
            })
    logger.success(f"{len(facilities)} facilities matched -> {fac_path}")

    # ---- Download: DocuWare docs -----------------------------------------------
    all_docs: List[Tuple[str, Dict[str, object], str]] = []
    for bid, f in sorted(facilities.items()):
        mt = "; ".join(sorted(f["matched_terms"]))  # type: ignore[arg-type]
        for d in f["docs"].values():  # type: ignore[union-attr]
            all_docs.append((bid, d, mt))
    # Name-/SIC-/BOA-matched facilities first (address-only matches last), newest documents first.
    all_docs.sort(key=lambda x: str(x[1]["item_date"] or ""), reverse=True)
    all_docs.sort(key=lambda x: (0 if re.search(r"(^|; )(name|boa):", x[2]) else 1, x[0]))
    logger.info(f"DocuWare documents across matched facilities: {len(all_docs)} "
                f"(downloading categories {sorted(wanted_categories)})")

    stop = False
    for bid, d, mt in all_docs:
        key = ("docuware", str(d["doc_id"]))
        prev = done.get(key)
        code = str(d["category_code"])
        label = CATEGORY_LABELS.get(code, "")
        row = {"source": "docuware", "doc_id": d["doc_id"], "bureau_id": bid if not bid.startswith("iepaid:") else "",
               "iepaid": d["iepaid"], "site_name": d["site_name"], "permit_id": d["permit_id"],
               "item_date": d["item_date"], "category_code": code, "category_label": label, "pages": d["pages"],
               "boa_permit_type": "", "boa_submittal_type": "", "boa_doc_type": "", "matched_terms": mt,
               "document_url": f"{DW_HOST}{DW_PLATFORM}/FileCabinets/{DW_CABINET}/Documents/{d['doc_id']}",
               "local_path": "", "status": "", "note": ""}
        if code not in wanted_categories:
            if prev is None:
                row["status"] = "listed_not_downloaded"
                row["note"] = "category not selected"
                index.append(row)
                listed += 1
            continue
        if prev in ("downloaded", "skipped_in_archive", "exists"):
            continue
        pair = (bid, str(d["permit_id"]))
        if code == "03K" and pair in archive_final_pairs and not args.redownload_archived:
            row["status"] = "skipped_in_archive"
            row["note"] = "final permit for this facility+permit already in il_epa_boa_archive"
            index.append(row)
            skipped_archive += 1
            continue
        stem = safe_stem(f"il_dw_{bid}_{d['permit_id'] or 'nopermit'}_{d['doc_id']}_{label or code}")
        dest = pdf_dir / f"{stem}.pdf"
        if dest.exists():
            row["status"], row["local_path"] = "exists", str(dest)
            index.append(row)
            continue
        if args.no_download:
            row["status"] = "listed"
            index.append(row)
            listed += 1
            continue
        if downloaded >= budget:
            stop = True
            break
        ok, err = dw.download(str(d["doc_id"]), dest)
        if ok:
            downloaded += 1
            row["status"], row["local_path"] = "downloaded", str(dest)
            logger.info(f"[{downloaded}/{budget}] {d['site_name']} {d['permit_id']} ({d['item_date']}, {d['pages']} pp) -> {dest.name}")
        else:
            failed += 1
            row["status"], row["note"] = "failed", err
            logger.warning(f"download failed {d['doc_id']} ({d['site_name']}): {err}")
        index.append(row)

    # ---- Download: BOA public-notice docs -------------------------------------
    if boa_rows and not stop:
        pairs_done: Set[Tuple[str, str]] = set()
        for r in boa_rows:
            fid, pid = str(r["FacilityId"]).strip(), str(r["PermitId"]).strip()
            if (fid, pid) in pairs_done:
                continue
            pairs_done.add((fid, pid))
            try:
                docs = boa.documents(fid, pid)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"BOA documents {fid}/{pid}: {e}")
                continue
            for doc in docs:
                pnid = str(doc.get("PublicNoticeId") or "").strip()
                if not pnid:
                    continue
                key = ("boa_archive", pnid)
                prev = done.get(key)
                if prev in ("downloaded", "skipped_in_archive", "exists"):
                    continue
                doc_type = str(doc.get("PublicNoticeType") or "document").strip()
                row = {"source": "boa_archive", "doc_id": pnid, "bureau_id": fid, "iepaid": "",
                       "site_name": str(r.get("FacilityName") or ""), "permit_id": pid,
                       "item_date": (str(doc.get("DocumentDate") or "")[:10]), "category_code": "",
                       "category_label": "", "pages": "", "boa_permit_type": str(doc.get("PermitType") or r.get("PermitType") or ""),
                       "boa_submittal_type": str(doc.get("SubmittalType") or ""), "boa_doc_type": doc_type,
                       "matched_terms": f"boa:{r['_reason']}", "document_url": f"{BOA_API}GetAirPermitDocument/{pnid}",
                       "local_path": "", "status": "", "note": ""}
                if pnid in archive_ids:
                    row["status"] = "skipped_in_archive"
                    row["note"] = "PublicNoticeId already in il_epa_boa_archive"
                    index.append(row)
                    skipped_archive += 1
                    continue
                dest = pdf_dir / f"{safe_stem(f'il_{fid}_{pid}_{pnid}_{doc_type}')}.pdf"
                if dest.exists():
                    row["status"], row["local_path"] = "exists", str(dest)
                    index.append(row)
                    continue
                if args.no_download:
                    row["status"] = "listed"
                    index.append(row)
                    listed += 1
                    continue
                if downloaded >= budget:
                    stop = True
                    break
                ok, err = boa.download(pnid, dest)
                if ok:
                    downloaded += 1
                    row["status"], row["local_path"] = "downloaded", str(dest)
                    logger.info(f"[{downloaded}/{budget}] BOA {r.get('FacilityName')} {pid} {doc_type} -> {dest.name}")
                else:
                    failed += 1
                    row["status"], row["note"] = "failed", err
                    logger.warning(f"BOA download failed {pnid}: {err}")
                index.append(row)
            if stop:
                break

    logger.info("=" * 60)
    logger.info("ILLINOIS EPA DATA-CENTER PERMIT PULL COMPLETE" + (" (budget reached)" if stop else ""))
    logger.info("=" * 60)
    logger.info(f"Facilities matched: {len(facilities)}")
    logger.info(f"Downloaded: {downloaded}, failed: {failed}, skipped (already in BOA archive): {skipped_archive}, "
                f"listed only: {listed}")
    logger.info(f"Index: {index.path} ({len(index.rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
