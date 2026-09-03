#!/usr/bin/env python3
"""Download Iowa DNR air-permit documents for data centers.

Iowa DNR's Air Quality Bureau exposes three unauthenticated, plain-HTTP
sources (no Selenium needed — every request shape below was verified with
curl/requests before this script was written):

1. AQRR construction-permit search (https://idnraqrr.iowadnr.gov, ASP.NET Core
   + jQuery, endpoints published as ``data-request-url`` divs on the page):
     GET /ConPermitSearch/ConstructionPermitPlantNameAutoComplete
         -> JSON list of EVERY permitted plant name (~3.3K; ?prefix= narrows it)
     GET /ConPermitSearch/ByConstructionPermitPlantNameView?prefix=<name>
     GET /ConPermitSearch/ByConstructionPermitPlantNumberView?prefix=<plant no>
         -> HTML rows (prefix match) with data-selectclick=<facilityId>,
            plant name, city, plant number (Iowa "facility file number", 78-01-121)
     GET /ConPermitSearch/GetConstructionPermitsByCity?searchString=<CITY>
         -> HTML rows of plants in a city
     GET /ConPermitSearch/ConstructionPermit?id=<facilityId>
         -> HTML table with one row per construction permit (permit no, project
            no, emission point, equipment description, issue date) and a
            /ConPermitSearch/DownloadFile?dataString=<urlencoded JSON>&type=PERMIT_FILLED_OBJECT
            link. Pre-EASY-Air permits are stored PDFs; EASY-Air-era rows 302 to
            programs.iowadnr.gov/easyair/.../StreamDoc.ashx?id=<easyAirPermitID>,
            which serves ONE "Collection of Air Permits" PDF shared by every
            permit in the project (verified identical hashes).
2. OpenText document search (https://programs.iowadnr.gov/docsearch, ASP.NET
   Core + DataTables server-side; the DNR's central document repository):
     POST /docsearch/Home/OTCSSearch  (form-encoded DataTables paging params
          draw/start/length/order plus viewModel[programfilter]=AirQuality,
          viewModel[aqsubfilter]=*AQB* | "AQB - Construction Permits" |
          "AQB - Title V Operating Permit" | ..., viewModel[facilityIDfilter]=
          <plant no>, viewModel[facilityNamefilter]=<contains>, ...)
          -> JSON {recordsTotal, data:[{object_ID, name, program, documentType,
             facilityID, facilityName, permitNumber, projectNumber,
             documentDate, mimeType, ...}]}.  No anti-forgery token needed.
          Pages larger than 200 rows return HTTP 500, so page at 200.
     GET  /docsearch/Home/OTCSDownload?objectID=<object_ID>&name=<name> -> file
3. Title V draft/final permit list — a static Drupal page
   (https://www.iowadnr.gov/environmental-protection/air-quality/operating-permits/draft-final-permits)
   whose entries link /media/<id>/download?inline.

Facilities are discovered by matching dc_targets terms (plus Iowa-specific
permit-entity aliases: Google = "Gable Corporation"/"Questa LLC", Meta =
"Siculus", Microsoft = "Osmium"/"Alluvium"/"Ginger"/"Willow Creek") against the
AQRR plant list, docsearch facility names, and an AQRR sweep of the cities in
the LBL data-center map.  For every matched plant number the script indexes
all docsearch documents (Construction Permits, Title V, Small Source /
Voluntary operating permits, Miscellaneous; optionally Compliance / Stack
Testing / MSEIQ), all AQRR construction-permit rows, and any Title V page
entries, then downloads PDFs in priority order under a global cap:

  tier 0  Title V page final/draft permits
  tier 1  docsearch operating-permit programs (Permit Package, Application, ...)
  tier 2  docsearch construction TSD-type docs (Engineering Evaluation,
          Application, DNR Review, Permit Package, Determination Letter,
          Modeling, Variance, Collection of Air Permits)
  tier 3  docsearch construction Final Permits, newest first, per-facility cap
  tier 4  docsearch Rescind / DNR Letter / Correspondence
  tier 5  AQRR permit rows not already covered by a docsearch document
          (EASY-Air rows collapsed to one per project), newest first

Compliance, stack-test and emissions-inventory documents are indexed (with
--index-all-programs) but never downloaded; zips are never downloaded.
Downloads are hash-deduplicated.  Raw responses are cached under cache/ so
re-runs are cheap and the whole run is resumable; the document index is
appended row by row and consolidated at exit.

Output layout
-------------
  <RAW_DATA_DIR>/ia_dnr_dc_permits/
    pdfs/<plant no>_<source>_<permit|project|doctype>_<name>.pdf
    cache/                          raw AQRR HTML / docsearch JSON pages
    ia_dnr_dc_permits_index.csv     one row per candidate document (all sources)
    ia_dnr_dc_facilities.csv        matched facilities and how they matched
    ia_dnr_dc_aqrr_permits.csv      every AQRR construction-permit row
                                    (permit -> emission point / equipment)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urljoin

import requests
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

import dc_targets  # noqa: E402

AQRR = "https://idnraqrr.iowadnr.gov"
DOCSEARCH = "https://programs.iowadnr.gov/docsearch"
TITLEV_PAGE = ("https://www.iowadnr.gov/environmental-protection/air-quality/"
               "operating-permits/draft-final-permits")
IOWADNR = "https://www.iowadnr.gov"

OUTPUT_DIR = RAW_DATA_DIR / "ia_dnr_dc_permits"
PDF_DIR = OUTPUT_DIR / "pdfs"
CACHE_DIR = OUTPUT_DIR / "cache"
INDEX_PATH = OUTPUT_DIR / "ia_dnr_dc_permits_index.csv"
FACILITIES_PATH = OUTPUT_DIR / "ia_dnr_dc_facilities.csv"
AQRR_PERMITS_PATH = OUTPUT_DIR / "ia_dnr_dc_aqrr_permits.csv"

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0 Safari/537.36"}

# Iowa permit entities / code names that dc_targets' national aliases miss.
IA_EXTRA_TERMS = [
    "Gable Corporation", "Gable", "Questa",          # Google, Council Bluffs
    "Siculus",                                        # Meta, Altoona
    "Osmium", "Alluvium", "Alluvion", "Ginger West", "Ginger East",
    "Willow Creek", "Soteria", "Ruthenium", "Project Mountain",   # Microsoft
    "Apple Data Center", "Apple WKE",                 # Apple, Waukee
    "QTS Cedar Rapids",
    "Emerson Global Data Center",
    "Team Technologies",                              # Cologix Cedar Falls plant name
    "Data Center", "Data Centers", "Datacenter", "Colocation",
]
# Terms from dc_targets that are unsafe as word matches against Iowa plant
# names (telecom switch buildings, sign shops) or belong to other states.
TERM_DENYLIST = {"switch", "design llc"}
# Plant names that match a term but are not data centers.
EXCLUDE_NAME_PATTERN = re.compile(r"amazon\.com services", re.IGNORECASE)  # fulfillment centers
DC_NAME_PATTERN = re.compile(r"data ?cent|datacenter|colocation|server farm|hyperscale", re.IGNORECASE)

# docsearch Air Quality sub-programs
SUBPROGRAMS_DEFAULT = [
    "AQB - Construction Permits",
    "AQB - Title V Operating Permit",
    "AQB - Small Source Operating Permit",
    "AQB - Voluntary Operating Permit",
    "AQB - Miscellaneous",
]
SUBPROGRAMS_EXTRA = ["AQB - Compliance", "AQB - Stack Testing", "AQB - MSEIQ",
                     "AQB - Asbestos", "AQB - Planning"]
DS_PAGE = 200  # >200 rows -> HTTP 500

OPERATING_PROGRAMS = {"Title V Operating Permit", "Small Source Operating Permit",
                      "Voluntary Operating Permit"}
TIER2_TYPES = {"Engineering Evaluation", "Application", "DNR Review", "Permit Package",
               "Determination Letter", "Modeling", "Variance", "Collection of Air Permits"}
TIER4_TYPES = {"Rescind", "DNR Letter", "Correspondence", "EMS Correspondence", "Misc"}
NEVER_DOWNLOAD_PROGRAMS = {"Compliance", "Stack Testing", "MSEIQ", "Asbestos", "Planning"}

INDEX_FIELDS = [
    "doc_key", "source", "facility_id", "facility_name", "city", "county", "match_terms",
    "program", "doc_type", "permit_number", "project_number", "doc_date", "doc_name",
    "mime", "download_url", "tier", "status", "pdf_path", "sha1", "bytes", "duplicate_of",
]
FACILITY_FIELDS = ["facility_id", "aqrr_facility_id", "names", "city", "county",
                   "match_terms", "sources", "n_docsearch_docs", "n_aqrr_permits"]
AQRR_FIELDS = ["facility_id", "aqrr_facility_id", "plant_name", "city", "county",
               "permit_number", "project_number", "emission_point", "equipment_description",
               "issue_date", "rescission_date", "file_name", "easyair_permit_id",
               "download_url", "docsearch_doc_key"]

# ----------------------------------------------------------------------------- helpers


def slug(s: str, n: int = 80) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s or "").strip("_")[:n]


def word_match(term: str, text: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
                     text, re.IGNORECASE) is not None


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def parse_date(s: str) -> str:
    """Normalise '9/18/2019', '2019-09-18T00:00:00' -> '2019-09-18' ('' if unknown)."""
    if not s:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            return "" if d.year <= 1900 else d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


class Client:
    """requests session with retries, polite delay and a response cache."""

    def __init__(self, delay: float, timeout: int, refresh: bool):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay, self.timeout, self.refresh = delay, timeout, refresh
        self.n_requests = 0
        self._ds_primed = False

    def _sleep(self):
        time.sleep(self.delay)

    def request(self, method: str, url: str, tries: int = 3, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        last = None
        for attempt in range(1, tries + 1):
            try:
                self.n_requests += 1
                r = self.s.request(method, url, **kw)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} for {url}")
                r.raise_for_status()
                self._sleep()
                return r
            except Exception as e:  # noqa: BLE001
                last = e
                logger.warning(f"attempt {attempt}/{tries} failed: {e}")
                time.sleep(self.delay * 2 * attempt + 2)
        raise RuntimeError(f"giving up on {url}: {last}")

    def cached_text(self, key: str, fetch) -> str:
        path = CACHE_DIR / key
        if path.exists() and not self.refresh:
            return path.read_text(encoding="utf-8")
        text = fetch()
        path.write_text(text, encoding="utf-8")
        return text

    # ---- AQRR ---------------------------------------------------------------
    def aqrr_get(self, path: str, params: Dict[str, str], timeout: Optional[int] = None) -> str:
        return self.request("GET", AQRR + path, params=params,
                            headers={"X-Requested-With": "XMLHttpRequest"},
                            timeout=timeout or self.timeout).text

    def aqrr_plant_names(self) -> List[str]:
        # Without a ``prefix`` the endpoint returns every plant name (~3.3K).
        txt = self.cached_text("aqrr_plantnames_all.json", lambda: self.aqrr_get(
            "/ConPermitSearch/ConstructionPermitPlantNameAutoComplete", {}))
        return json.loads(txt)

    def aqrr_lookup(self, prefix: str, by: str = "name") -> List[Dict[str, str]]:
        view = ("/ConPermitSearch/ByConstructionPermitPlantNameView" if by == "name"
                else "/ConPermitSearch/ByConstructionPermitPlantNumberView")
        html = self.cached_text(f"aqrr_lookup_{by}_{slug(prefix)}.html",
                                lambda: self.aqrr_get(view, {"prefix": prefix}))
        return parse_aqrr_plant_rows(html)

    def aqrr_city(self, city: str) -> List[Dict[str, str]]:
        html = self.cached_text(f"aqrr_city_{slug(city)}.html", lambda: self.aqrr_get(
            "/ConPermitSearch/GetConstructionPermitsByCity", {"searchString": city.upper()}))
        return parse_aqrr_plant_rows(html)

    def aqrr_permits_page(self, aqrr_id: str) -> str:
        return self.cached_text(f"aqrr_permits_{aqrr_id}.html", lambda: self.request(
            "GET", f"{AQRR}/ConPermitSearch/ConstructionPermit", params={"id": aqrr_id},
            timeout=max(self.timeout, 600)).text)

    # ---- docsearch ------------------------------------------------------------
    DS_COLS = ["name", "program", "documentID", "documentDate", "documentType", "notes",
               "facilityID", "permitNumber", "facilityName", "projectNumber",
               "facilityCity", "facilityCounty"]

    def _ds_prime(self):
        if not self._ds_primed:
            self.request("GET", f"{DOCSEARCH}/Home/Search")
            self._ds_primed = True

    def ds_search_page(self, start: int, length: int, **vm) -> Dict:
        self._ds_prime()
        model = {
            "programfilter": "AirQuality", "aqsubfilter": "*AQB*",
            "damsafetysubfilter": "*LQB - Dam Safety*", "floodplainsubfilter": "*LQB - Flood*",
            "docidfilter": "", "facilityIDfilter": "", "facilityNamefilter": "",
            "permitNumberfilter": "", "projectNumberfilter": "", "facilityCityfilter": "",
            "facilityCountyfilter": "", "typefilter": "", "notesfilter": "",
            "documentDateFilter": "", "documentDateEndFilter": "",
            "documentTypeDescriptionFilter": "", "legalAttorneyFilter": "", "legalAreaFilter": "",
            "section": "", "township": "", "range": "", "limitFilter": "0",
        }
        model.update(vm)
        data = {"draw": "1", "start": str(start), "length": str(length), "search[value]": "",
                "search[regex]": "false", "order[0][column]": "3", "order[0][dir]": "desc"}
        for i, c in enumerate(self.DS_COLS):
            data[f"columns[{i}][data]"] = c
            data[f"columns[{i}][name]"] = ""
            data[f"columns[{i}][searchable]"] = "true"
            data[f"columns[{i}][orderable]"] = "true"
            data[f"columns[{i}][search][value]"] = ""
            data[f"columns[{i}][search][regex]"] = "false"
        for k, v in model.items():
            data[f"viewModel[{k}]"] = v
        r = self.request("POST", f"{DOCSEARCH}/Home/OTCSSearch", data=data, headers={
            "X-Requested-With": "XMLHttpRequest", "Referer": f"{DOCSEARCH}/Home/Search",
            "__RequestVerificationToken": ""}, timeout=max(self.timeout, 180))
        return r.json()

    def ds_search_all(self, cache_key: str, max_pages: int = 100, **vm) -> List[Dict]:
        """Page through OTCSSearch (200 rows/page) with per-page caching."""
        rows: List[Dict] = []
        for page in range(max_pages):
            key = f"{cache_key}_p{page}.json"
            txt = self.cached_text(key, lambda: json.dumps(
                self.ds_search_page(page * DS_PAGE, DS_PAGE, **vm)))
            data = json.loads(txt).get("data", [])
            rows.extend(data)
            if len(data) < DS_PAGE:
                break
        return rows

    # ---- downloads -------------------------------------------------------------
    def download(self, url: str) -> requests.Response:
        return self.request("GET", url, timeout=max(self.timeout, 300), allow_redirects=True)


def parse_aqrr_plant_rows(html: str) -> List[Dict[str, str]]:
    """Rows of the plant-lookup / city views: Select | name | [city] | plant no."""
    out = []
    for m in re.finditer(r'data-selectclick="(\d+)"[^>]*>Select</a>\s*</td>(.*?)</tr>', html, re.S):
        tds = [unescape(strip_tags(t)) for t in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)]
        if len(tds) >= 3:
            name, city, number = tds[0], tds[1], tds[2]
        elif len(tds) == 2:
            name, city, number = tds[0], "", tds[1]
        else:
            continue
        out.append({"aqrr_facility_id": m.group(1), "plant_name": name, "city": city,
                    "facility_id": number})
    return out


def parse_aqrr_permits(html: str) -> Tuple[Dict[str, str], List[Dict]]:
    header = {}
    m = re.search(r'id="plantHeader".*?</div>\s*</div>', html, re.S)
    if m:
        pairs = re.findall(r'<div class="col-5 text-end">\s*(.*?):\s*</div>\s*<div class="col-7 text-start">\s*(.*?)\s*</div>',
                           m.group(0), re.S)
        header = {k.strip(): unescape(strip_tags(v)) for k, v in pairs}
    rows = []
    for link in re.findall(r'href="(/ConPermitSearch/DownloadFile\?dataString=[^"]+)"', html):
        link = unescape(link)
        try:
            d = json.loads(unquote(link.split("dataString=")[1].split("&type=")[0]))
        except Exception:  # noqa: BLE001
            continue
        d["download_url"] = AQRR + link
        rows.append(d)
    return header, rows


def parse_titlev_page(html: str) -> List[Dict[str, str]]:
    body = re.search(r"<main.*?</main>", html, re.S)
    body = body.group(0) if body else html
    di, fi = body.find("<h2>Draft Permits</h2>"), body.find("<h2>Final Permits</h2>")
    out = []
    if 0 <= di < fi:
        for tr in re.findall(r"<tr>(.*?)</tr>", body[di:fi], re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(tds) < 3:
                continue
            href = re.search(r'href="([^"]+)"', tds[0])
            label = re.search(r'aria-label="([^"]*)"', tds[0])
            name = unescape(re.sub(r"\s+", " ", label.group(1) if label else strip_tags(tds[0])))
            name = re.sub(r"\s*\(\d[\d.]* ?[KM]B\).*$", "", name).strip()
            out.append({"kind": "draft", "url": urljoin(IOWADNR, href.group(1)) if href else "",
                        "name": name, "city": strip_tags(tds[1]), "date": strip_tags(tds[2])})
    if fi >= 0:
        for m in re.finditer(r'<a[^>]*href="(/media/\d+/download[^"]*)"[^>]*aria-label="([^"]*)"',
                             body[fi:]):
            name = unescape(re.sub(r"\s+", " ", m.group(2)))
            name = re.sub(r"\s*\(\d[\d.]* ?[KM]B\).*$", "", name).strip()
            city = name.rsplit(" - ", 1)[1] if " - " in name else ""
            out.append({"kind": "final", "url": urljoin(IOWADNR, m.group(1)), "name": name,
                        "city": city, "date": ""})
    return out


def append_row(path: Path, fields: List[str], row: Dict) -> None:
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def rewrite_rows(path: Path, fields: List[str], rows: Iterable[Dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


def load_index() -> Dict[str, Dict]:
    rows: Dict[str, Dict] = {}
    if INDEX_PATH.exists():
        with open(INDEX_PATH, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["doc_key"]] = r  # last write wins
    return rows


# ----------------------------------------------------------------------------- discovery


def build_terms(args) -> List[str]:
    if args.terms:
        return [t.strip() for t in args.terms.split(",") if t.strip()]
    terms = dc_targets.entity_terms("IA") + IA_EXTRA_TERMS
    seen, out = set(), []
    for t in terms:
        k = t.lower()
        if k in TERM_DENYLIST or k in seen or len(t) < 4:
            continue
        seen.add(k)
        out.append(t)
    return out


def discover(client: Client, terms: List[str], args) -> Dict[str, Dict]:
    """plant number -> facility record."""
    fac: Dict[str, Dict] = {}

    def add(fid: str, name: str, city: str, term: str, source: str, aqrr_id: str = "",
            county: str = ""):
        fid = (fid or "").strip()
        if not fid or not re.match(r"^\d{2}-\d{2}-\d{3}", fid):
            return
        if fid in args.exclude_ids:
            return
        if EXCLUDE_NAME_PATTERN.search(name or ""):
            logger.info(f"excluded by name pattern: {fid} {name!r}")
            return
        rec = fac.setdefault(fid, {"facility_id": fid, "aqrr_facility_id": "", "names": set(),
                                   "city": "", "county": "", "match_terms": set(), "sources": set()})
        if name:
            rec["names"].add(name.strip())
        rec["city"] = rec["city"] or city
        rec["county"] = rec["county"] or county
        rec["match_terms"].add(term)
        rec["sources"].add(source)
        if aqrr_id:
            rec["aqrr_facility_id"] = aqrr_id

    # 1. AQRR full plant list, whole-word term match, resolve to plant numbers.
    names = client.aqrr_plant_names()
    logger.info(f"AQRR plant list: {len(names)} names")
    hits = []
    for n in names:
        matched = [t for t in terms if word_match(t, n)]
        if DC_NAME_PATTERN.search(n):
            matched.append("<dc-name-pattern>")
        if matched:
            hits.append((n, matched))
    logger.info(f"AQRR name matches: {len(hits)}")
    for n, matched in hits:
        try:
            rows = client.aqrr_lookup(n, by="name")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"AQRR lookup failed for {n!r}: {e}")
            continue
        for r in rows:
            if r["plant_name"].strip().lower() == n.strip().lower() or len(rows) == 1:
                add(r["facility_id"], r["plant_name"], r["city"], ",".join(matched), "aqrr",
                    r["aqrr_facility_id"])

    # 2. AQRR city sweep over LBL-map cities: auto-include DC-named plants only.
    if not args.no_city_sweep:
        df = dc_targets.load_combined()
        cities = sorted({str(c).strip() for c in df[df["state"].astype(str).str.upper() == "IA"]["city"].dropna()})
        for city in cities:
            try:
                rows = client.aqrr_city(city)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"AQRR city sweep failed for {city}: {e}")
                continue
            for r in rows:
                if DC_NAME_PATTERN.search(r["plant_name"]) or any(word_match(t, r["plant_name"]) for t in terms):
                    add(r["facility_id"], r["plant_name"], city, "<city-sweep>", "aqrr-city",
                        r["aqrr_facility_id"])
            logger.debug(f"city {city}: {len(rows)} plants")

    # 3. docsearch facility-name contains-search per term (catches historical names).
    if not args.no_docsearch_discovery:
        for i, t in enumerate(terms, 1):
            try:
                rows = client.ds_search_all(f"ds_name_{slug(t)}", max_pages=args.discovery_pages,
                                            facilityNamefilter=t)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{i}/{len(terms)}] docsearch name search failed for {t!r}: {e}")
                continue
            found = Counter((r.get("facilityID") or "", r.get("facilityName") or "",
                             r.get("facilityCity") or "", r.get("facilityCounty") or "") for r in rows)
            n_new = 0
            for (fid, name, city, county), _ in found.items():
                if not word_match(t, name):
                    continue  # server "contains" matched inside a word
                if fid not in fac:
                    n_new += 1
                add(fid, name, city, t, "docsearch", county=county)
            if rows:
                logger.info(f"[{i}/{len(terms)}] docsearch {t!r}: {len(rows)} docs, "
                            f"{len(found)} facility names, {n_new} new facilities")

    # 4. Manual additions.
    for fid in args.facility_ids:
        add(fid, "", "", "<manual>", "manual")

    # Resolve AQRR facility ids (and canonical name/city) for docsearch-only facilities.
    for fid, rec in fac.items():
        if rec["aqrr_facility_id"]:
            continue
        try:
            rows = [r for r in client.aqrr_lookup(fid, by="number") if r["facility_id"] == fid]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"AQRR number lookup failed for {fid}: {e}")
            continue
        if rows:
            rec["aqrr_facility_id"] = rows[0]["aqrr_facility_id"]
            rec["names"].add(rows[0]["plant_name"])
            rec["city"] = rec["city"] or rows[0]["city"]
    return fac


# ----------------------------------------------------------------------------- indexing


def tier_for(program: str, doc_type: str) -> Optional[int]:
    if program in NEVER_DOWNLOAD_PROGRAMS:
        return None
    if program in OPERATING_PROGRAMS:
        return None if doc_type == "Fee Doc" else 1
    if program == "Construction Permits":
        if doc_type in TIER2_TYPES:
            return 2
        if doc_type == "Final Permit":
            return 3
        return 4
    if program == "Miscellaneous":
        return 4
    return 4


def index_docsearch(client: Client, fac: Dict[str, Dict], args) -> List[Dict]:
    docs: List[Dict] = []
    subs = SUBPROGRAMS_DEFAULT + (SUBPROGRAMS_EXTRA if args.index_all_programs else [])
    for i, (fid, rec) in enumerate(sorted(fac.items()), 1):
        n = 0
        for sub in subs:
            try:
                rows = client.ds_search_all(f"ds_fac_{fid}_{slug(sub)}", facilityIDfilter=fid,
                                            aqsubfilter=sub)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"docsearch index failed for {fid} / {sub}: {e}")
                continue
            for r in rows:
                if r.get("facilityID") != fid:
                    continue
                name = r.get("name") or ""
                rec["names"].add((r.get("facilityName") or "").strip())
                rec["city"] = rec["city"] or (r.get("facilityCity") or "")
                rec["county"] = rec["county"] or (r.get("facilityCounty") or "")
                docs.append({
                    "doc_key": f"ds:{r.get('object_ID')}",
                    "source": "docsearch", "facility_id": fid,
                    "facility_name": r.get("facilityName") or "",
                    "city": r.get("facilityCity") or "", "county": r.get("facilityCounty") or "",
                    "match_terms": ";".join(sorted(rec["match_terms"])),
                    "program": r.get("program") or "", "doc_type": r.get("documentType") or "",
                    "permit_number": (r.get("permitNumber") or "").strip(),
                    "project_number": (r.get("projectNumber") or "").strip(),
                    "doc_date": parse_date(r.get("documentDate") or ""),
                    "doc_name": name, "mime": r.get("mimeType") or "",
                    "download_url": f"{DOCSEARCH}/Home/OTCSDownload?objectID={r.get('object_ID')}&name={requests.utils.quote(name)}",
                    "tier": tier_for(r.get("program") or "", r.get("documentType") or ""),
                })
                n += 1
        rec["n_docsearch_docs"] = n
        logger.info(f"[{i}/{len(fac)}] docsearch {fid} {sorted(rec['names'])[:1]}: {n} documents")
    return docs


def index_aqrr(client: Client, fac: Dict[str, Dict], ds_docs: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Returns (aqrr permit rows, AQRR download candidates not covered by docsearch)."""
    covered: Dict[str, set] = defaultdict(set)  # fid -> permit numbers / project numbers seen in docsearch
    ds_key_by_permit: Dict[Tuple[str, str], str] = {}
    for d in ds_docs:
        for k in (d["permit_number"], d["project_number"]):
            if k and k != "0":
                covered[d["facility_id"]].add(k.upper())
                ds_key_by_permit.setdefault((d["facility_id"], k.upper()), d["doc_key"])
    permits: List[Dict] = []
    candidates: List[Dict] = []
    for i, (fid, rec) in enumerate(sorted(fac.items()), 1):
        aid = rec.get("aqrr_facility_id")
        if not aid:
            rec["n_aqrr_permits"] = 0
            continue
        try:
            header, rows = parse_aqrr_permits(client.aqrr_permits_page(aid))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"AQRR permits page failed for {fid} (id {aid}): {e}")
            continue
        rec["names"].add(header.get("Plant Name", "").strip())
        rec["city"] = rec["city"] or header.get("City Name", "")
        rec["county"] = rec["county"] or header.get("County Name", "")
        rec["n_aqrr_permits"] = len(rows)
        logger.info(f"[{i}/{len(fac)}] AQRR {fid} {header.get('Plant Name', '')!r}: {len(rows)} permit rows")
        seen_projects: set = set()
        for r in rows:
            pn = (r.get("permitNumber") or "").strip()
            proj = (r.get("projectNumber") or "").strip()
            key = ds_key_by_permit.get((fid, pn.upper())) or ds_key_by_permit.get((fid, proj.upper()), "")
            permits.append({
                "facility_id": fid, "aqrr_facility_id": aid,
                "plant_name": header.get("Plant Name", ""), "city": header.get("City Name", ""),
                "county": header.get("County Name", ""), "permit_number": pn,
                "project_number": proj, "emission_point": r.get("emissionPointID") or "",
                "equipment_description": r.get("emissionPointDescription") or "",
                "issue_date": parse_date(r.get("issueDate") or ""),
                "rescission_date": parse_date(r.get("rescissionDate") or ""),
                "file_name": r.get("fileName") or "",
                "easyair_permit_id": r.get("easyAirPermitID") or "",
                "download_url": r["download_url"], "docsearch_doc_key": key,
            })
            if key:
                continue
            if r.get("easyAirPermitID"):
                # One collection PDF per project: keep a single candidate per project.
                if proj in seen_projects:
                    continue
                seen_projects.add(proj)
                doc_key, name = f"aqrr:ea:{r['easyAirPermitID']}", f"easyair_{r['easyAirPermitID']}"
            elif r.get("fileName"):
                doc_key, name = f"aqrr:fn:{fid}:{r['fileName']}", r["fileName"]
            else:
                continue
            candidates.append({
                "doc_key": doc_key, "source": "aqrr", "facility_id": fid,
                "facility_name": header.get("Plant Name", ""), "city": header.get("City Name", ""),
                "county": header.get("County Name", ""),
                "match_terms": ";".join(sorted(rec["match_terms"])),
                "program": "Construction Permits", "doc_type": "Final Permit (AQRR)",
                "permit_number": pn, "project_number": proj,
                "doc_date": parse_date(r.get("issueDate") or ""), "doc_name": name,
                "mime": "application/pdf", "download_url": r["download_url"], "tier": 5,
            })
    return permits, candidates


def index_titlev(client: Client, fac: Dict[str, Dict], terms: List[str]) -> List[Dict]:
    html = client.cached_text("titlev_page.html", lambda: client.request("GET", TITLEV_PAGE).text)
    entries = parse_titlev_page(html)
    logger.info(f"Title V page: {len(entries)} entries")
    norm_names = []
    for fid, rec in fac.items():
        for n in rec["names"]:
            core = re.sub(r"[^a-z0-9 ]", " ", n.lower()).split(" - ")[0].strip()
            if len(core) >= 5:
                norm_names.append((core, fid, rec))
    out = []
    for e in entries:
        name = e["name"]
        matched = [t for t in terms if word_match(t, name)]
        if DC_NAME_PATTERN.search(name):
            matched.append("<dc-name-pattern>")
        fid, rec = "", None
        lname = re.sub(r"[^a-z0-9 ]", " ", name.lower())
        for core, f, r in norm_names:
            if lname.startswith(core) or core.startswith(lname.split(" - ")[0].strip()):
                fid, rec = f, r
                break
        if not matched and not fid:
            continue
        if EXCLUDE_NAME_PATTERN.search(name):
            continue
        media_id = re.search(r"/media/(\d+)/", e["url"])
        out.append({
            "doc_key": f"tv:{media_id.group(1) if media_id else slug(name)}",
            "source": "titlev_page", "facility_id": fid, "facility_name": name.rsplit(" - ", 1)[0],
            "city": e["city"], "county": rec["county"] if rec else "",
            "match_terms": ";".join(sorted(set(matched) | (rec["match_terms"] if rec else set()))),
            "program": "Title V Operating Permit", "doc_type": f"{e['kind'].title()} Permit (web page)",
            "permit_number": "", "project_number": "", "doc_date": parse_date(e["date"]),
            "doc_name": name, "mime": "application/pdf", "download_url": e["url"], "tier": 0,
        })
    logger.info(f"Title V page: {len(out)} data-center entries")
    return out


# ----------------------------------------------------------------------------- download


def dest_path(doc: Dict) -> Path:
    fid = doc["facility_id"] or "unknown"
    if doc["source"] == "docsearch":
        stem = f"{fid}_ds_{slug(doc['doc_type'], 30)}_{slug(doc['permit_number'] or doc['project_number'], 20)}_{slug(Path(doc['doc_name']).stem, 40)}"
    elif doc["source"] == "aqrr":
        stem = f"{fid}_aqrr_{slug(doc['project_number'], 12)}_{slug(Path(doc['doc_name']).stem, 40)}"
    else:
        stem = f"{fid or slug(doc['facility_name'], 30)}_titlev_{doc['doc_key'].split(':')[-1]}"
    stem = re.sub(r"_+", "_", stem).strip("_")
    ext = ".pdf"
    if "wordprocessingml" in doc.get("mime", ""):
        ext = ".docx"
    return PDF_DIR / f"{stem}{ext}"


def run_downloads(client: Client, docs: List[Dict], index: Dict[str, Dict], args) -> None:
    # Existing unique files (for the global cap) and hash map for dedupe.
    sha_to_path: Dict[str, str] = {}
    n_unique = 0
    for r in index.values():
        if r.get("status") in ("downloaded", "exists") and r.get("pdf_path") and Path(r["pdf_path"]).exists():
            n_unique += 1
            if r.get("sha1"):
                sha_to_path.setdefault(r["sha1"], r["pdf_path"])

    def sort_key(d):
        return (d["tier"] if d["tier"] is not None else 99, d["doc_date"] and -int(d["doc_date"].replace("-", "")) or 0,
                d["facility_id"], d["doc_name"])

    todo = sorted([d for d in docs if d["tier"] is not None], key=sort_key)
    per_fac_final: Counter = Counter()
    for r in index.values():
        if r.get("status") in ("downloaded", "exists") and r.get("tier") == "3":
            per_fac_final[r["facility_id"]] += 1

    n_attempts = 0
    for d in todo:
        prev = index.get(d["doc_key"])
        if prev and prev.get("status") in ("downloaded", "exists", "duplicate", "non_pdf"):
            continue
        if args.no_download:
            continue
        if d["mime"] and ("zip" in d["mime"] or "gzip" in d["mime"]):
            d.update(status="skipped_zip")
            index[d["doc_key"]] = d
            append_row(INDEX_PATH, INDEX_FIELDS, d)
            continue
        if "pdf" not in (d["mime"] or "pdf") and not args.allow_non_pdf:
            d.update(status="skipped_mime")
            index[d["doc_key"]] = d
            append_row(INDEX_PATH, INDEX_FIELDS, d)
            continue
        if d["tier"] == 3 and per_fac_final[d["facility_id"]] >= args.per_facility_cap:
            d.update(status="skipped_facility_cap")
            index[d["doc_key"]] = d
            append_row(INDEX_PATH, INDEX_FIELDS, d)
            continue
        if n_unique >= args.max_downloads:
            d.update(status="skipped_cap")
            index[d["doc_key"]] = d
            append_row(INDEX_PATH, INDEX_FIELDS, d)
            continue
        if args.limit and n_attempts >= args.limit:
            logger.info(f"--limit {args.limit} reached")
            break
        dest = dest_path(d)
        if dest.exists():
            data = dest.read_bytes()
            d.update(status="exists", pdf_path=str(dest), sha1=hashlib.sha1(data).hexdigest(),
                     bytes=len(data))
            sha_to_path.setdefault(d["sha1"], str(dest))
            n_unique += 1
            if d["tier"] == 3:
                per_fac_final[d["facility_id"]] += 1
            index[d["doc_key"]] = d
            append_row(INDEX_PATH, INDEX_FIELDS, d)
            continue
        n_attempts += 1
        try:
            r = client.download(d["download_url"])
            content = r.content
            is_pdf = content.startswith(b"%PDF")
            is_docx = content[:2] == b"PK" and dest.suffix == ".docx"
            if not (is_pdf or (is_docx and args.allow_non_pdf)):
                ctype = r.headers.get("Content-Type", "")
                logger.warning(f"non-PDF response ({ctype}, {len(content)} B) for {d['doc_name']} [{d['doc_key']}]")
                d.update(status="non_pdf", bytes=len(content))
            else:
                sha = hashlib.sha1(content).hexdigest()
                if sha in sha_to_path:
                    d.update(status="duplicate", duplicate_of=sha_to_path[sha], sha1=sha, bytes=len(content))
                else:
                    dest.write_bytes(content)
                    sha_to_path[sha] = str(dest)
                    n_unique += 1
                    if d["tier"] == 3:
                        per_fac_final[d["facility_id"]] += 1
                    d.update(status="downloaded", pdf_path=str(dest), sha1=sha, bytes=len(content))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"download failed for {d['doc_name']} [{d['doc_key']}]: {e}")
            d.update(status="error")
        index[d["doc_key"]] = d
        append_row(INDEX_PATH, INDEX_FIELDS, d)
        if n_attempts % 25 == 0:
            logger.info(f"downloads: {n_attempts} attempted this run, {n_unique} unique files on disk")
    logger.success(f"downloads: {n_attempts} attempted this run, {n_unique} unique files on disk "
                   f"(cap {args.max_downloads})")


# ----------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--terms", help="Comma-separated search terms (replaces dc_targets + IA extras)")
    ap.add_argument("--facility-ids", default="", help="Comma-separated extra plant numbers (78-01-121)")
    ap.add_argument("--exclude-ids", default="", help="Comma-separated plant numbers to skip")
    ap.add_argument("--no-city-sweep", action="store_true")
    ap.add_argument("--no-docsearch-discovery", action="store_true")
    ap.add_argument("--discovery-pages", type=int, default=3,
                    help="docsearch pages (x200 docs) per term during discovery")
    ap.add_argument("--index-all-programs", action="store_true",
                    help="also index Compliance / Stack Testing / MSEIQ / Asbestos docs (never downloaded)")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max download attempts this run (tests)")
    ap.add_argument("--max-downloads", type=int, default=1500,
                    help="cap on unique PDFs on disk, all runs combined")
    ap.add_argument("--per-facility-cap", type=int, default=500,
                    help="max construction Final Permit PDFs per facility (newest first)")
    ap.add_argument("--allow-non-pdf", action="store_true", help="also save .docx documents")
    ap.add_argument("--delay", type=float, default=0.7)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--refresh", action="store_true", help="ignore cached responses")
    args = ap.parse_args()
    args.facility_ids = [x.strip() for x in args.facility_ids.split(",") if x.strip()]
    args.exclude_ids = {x.strip() for x in args.exclude_ids.split(",") if x.strip()}

    for d in (OUTPUT_DIR, PDF_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    logger.add(OUTPUT_DIR / "ia_dnr_dc_permits.log", level="INFO", enqueue=True)

    client = Client(args.delay, args.timeout, args.refresh)
    terms = build_terms(args)
    logger.info(f"{len(terms)} search terms")

    fac = discover(client, terms, args)
    logger.success(f"{len(fac)} matched facilities: " +
                   ", ".join(f"{fid} {sorted(r['names'])[0] if r['names'] else ''}" for fid, r in sorted(fac.items())))

    ds_docs = index_docsearch(client, fac, args)
    aqrr_permits, aqrr_docs = index_aqrr(client, fac, ds_docs)
    tv_docs = index_titlev(client, fac, terms)
    docs = tv_docs + ds_docs + aqrr_docs
    logger.success(f"{len(docs)} candidate documents indexed "
                   f"(docsearch {len(ds_docs)}, AQRR-only {len(aqrr_docs)}, Title V page {len(tv_docs)})")

    rewrite_rows(FACILITIES_PATH, FACILITY_FIELDS, ({
        **rec, "names": " | ".join(sorted(n for n in rec["names"] if n)),
        "match_terms": ";".join(sorted(rec["match_terms"])), "sources": ";".join(sorted(rec["sources"])),
        "n_docsearch_docs": rec.get("n_docsearch_docs", 0), "n_aqrr_permits": rec.get("n_aqrr_permits", 0),
    } for _, rec in sorted(fac.items())))
    rewrite_rows(AQRR_PERMITS_PATH, AQRR_FIELDS, aqrr_permits)
    logger.info(f"wrote {FACILITIES_PATH.name} ({len(fac)} rows) and {AQRR_PERMITS_PATH.name} ({len(aqrr_permits)} rows)")

    index = load_index()
    for d in docs:  # make sure every candidate is represented at least once
        d.setdefault("status", "")
        if d["doc_key"] not in index:
            row = {**d, "status": "indexed" if d["tier"] is not None else "not_downloadable"}
            index[d["doc_key"]] = row
            append_row(INDEX_PATH, INDEX_FIELDS, row)
    try:
        run_downloads(client, docs, index, args)
    finally:
        rewrite_rows(INDEX_PATH, INDEX_FIELDS, sorted(index.values(), key=lambda r: (
            str(r.get("tier") or 99), r.get("facility_id", ""), r.get("doc_key", ""))))
        counts = Counter(r.get("status") for r in index.values())
        logger.success(f"index consolidated: {len(index)} rows, statuses {dict(counts)}; "
                       f"{client.n_requests} HTTP requests this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
