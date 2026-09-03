#!/usr/bin/env python3
"""Download Wisconsin DNR (WARP) air-permit documents for data centers.

WARP (https://apps.dnr.wi.gov/warp_ext/) is classic ASP.NET WebForms; the
mechanics are the same __VIEWSTATE postbacks used by
download_wi_dnr_titlev_permits.py, whose parsers this script imports:

  1. AM_PermitTrackingSearch.aspx  POST __EVENTTARGET=...$btnSearch with
       txtSic/grpSic     (rdbSic3 = exact; 7374 = computer processing/data prep)
       txtNaics/grpNaics (rdbNaics3 = exact; 518210 = data processing/hosting)
       txtFacName/grpName=rdbFacName2   (facility-name CONTAINS)
       txtAddress/grpAddress=rdbAddress2 (+ ddlCity) (address CONTAINS)
     The radio value MUST be posted explicitly (omitted -> server defaults to
     exact match). Results come back in one unpaginated gvResult grid
     (FID | Facility Name | Address | City | County). If exactly ONE facility
     matches, the server 302-redirects straight to the facility page instead
     of rendering the grid.
  2. gvResult row lnkSelect postback -> 302 to AM_PermitTracking2.aspx?id=<seq>.
  3. Facility page: chkInactive postback reveals superseded permits;
     gvPermits Select$N reveals gvDocuments for that permit; gvDocuments
     Select$M re-renders that row with a sessionless
     AM_DownloadObject.aspx?id=<object_id> link.
  4. GET AM_DownloadObject.aspx?id=<object_id> -> PDF.

Facility discovery (union of):
  * SIC / NAICS code sweeps (data-center codes),
  * facility-name CONTAINS searches for data-center keywords, hyperscale /
    colo operators, and every WI operator term from dc_targets,
  * address CONTAINS + city searches for each WI facility in the LBL
    data-center map (address-only hits are tier C: indexed, not downloaded).

Every permit (active + inactive) and every document of each matched
facility is indexed; documents already present under
data/raw/wi_dnr_titlev_permits or data/raw/wi_dnr_air_permits are flagged
(status=skipped_existing) rather than re-downloaded. Both CSVs are appended
row by row and the run resumes from them.

Output layout
-------------
  <RAW_DATA_DIR>/wi_dnr_dc_permits/
    pdfs/<fid>_<object_id>_<doc_name>.pdf
    wi_dnr_dc_facilities.csv        matched facilities, tier + match terms
    wi_dnr_dc_permits_index.csv     one row per (permit, document)
    download_wi_dnr_dc_permits.log

Usage:
    python scripts/download_wi_dnr_dc_permits.py --limit 5 --facility-limit 2   # smoke test
    python scripts/download_wi_dnr_dc_permits.py --limit 1000                    # full pull
"""

from __future__ import annotations

import argparse
import csv
import html as htmllib
import http.cookiejar
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

import dc_targets  # noqa: E402
from download_wi_dnr_titlev_permits import (  # noqa: E402
    clean,
    grid_html,
    parse_doc_rows,
    parse_permit_rows,
    parse_result_rows,
    serialize_form,
)

BASE = "https://apps.dnr.wi.gov/warp_ext/"
SEARCH_URL = BASE + "AM_PermitTrackingSearch.aspx"
FACILITY_URL = BASE + "AM_PermitTracking2.aspx?id={seq}"
DOWNLOAD_URL = BASE + "AM_DownloadObject.aspx?id={oid}"
P = "ctl00$ContentPlaceHolder1$"
UA = "Mozilla/5.0 (research; permit dataset; contact alex.hubbard@berkeley.edu)"

OUTPUT_DIR = RAW_DATA_DIR / "wi_dnr_dc_permits"
PDF_DIR = OUTPUT_DIR / "pdfs"
FACILITIES_CSV = OUTPUT_DIR / "wi_dnr_dc_facilities.csv"
INDEX_CSV = OUTPUT_DIR / "wi_dnr_dc_permits_index.csv"
EXISTING_ROOTS = [RAW_DATA_DIR / "wi_dnr_titlev_permits", RAW_DATA_DIR / "wi_dnr_air_permits"]

# --- search seeds -----------------------------------------------------------
DEFAULT_SICS = ["7374"]          # exact match; 7374 = computer processing & data preparation
DEFAULT_NAICS = ["518210", "519130"]  # exact; data processing/hosting, internet publishing (Meta/Google)

DC_NAME_RE = re.compile(r"data ?cent|datacenter|colocat|\bcolo\b|\bserver|hyperscale", re.I)
DC_KEYWORD_TERMS = ["DATA CENTER", "DATACENTER", "COLOCATION", "SERVER", "HYPERSCALE"]

# Hyperscale / colo / crypto operators known or rumoured in Wisconsin; these
# are searched in addition to dc_targets.entity_terms("WI") and, when they
# hit, rank the facility tier A.
WI_OPERATOR_TERMS = [
    "Microsoft", "Fairwater", "Vantage", "Oracle", "OpenAI", "Lighthouse",
    "Meta Platforms", "Facebook", "Degas", "QTS", "Quality Technology",
    "Digital Realty", "FII ", "Foxconn", "EdgeConneX", "Expedient", "TierPoint",
    "OneNeck", "US Signal", "CSR Milwaukee", "1547", "Data Holdings", "Vaultas",
    "CyberLynk", "ServerFarm", "SupraNet", "Cloverleaf", "Oppidan", "Sazmining",
    "Metavante", "Cologix", "Flexential", "DataBank", "Compass Datacenters",
    "Aligned Data", "Stack Infrastructure", "Sabey", "T5 Data", "CoreSite",
    "Iron Mountain", "Crusoe", "CoreWeave", "Applied Digital", "Core Scientific",
    "Cipher Mining", "Riot Platforms", "Marathon Digital", "TeraWulf", "Hut 8",
    "Bitdeer", "Amazon Data Services", "VADATA", "Google", "Equinix", "CyrusOne",
    "Prime Data Centers", "EdgeCore", "Novva Data", "PowerHouse Data",
]
# Entity terms that hit non-data-center facilities in WI (fulfillment centres,
# telecom hubs, IT firms) -> tier B: downloaded by default but flagged.
TIER_B_HINTS = {"amazon", "apple inc", "ntt", "lumen", "windstream", "cogent",
                "charter", "at&t", "tds", "spectrum", "us signal"}
SKIP_TERMS = {"design llc", "wisconsin electric power company", "switch", "meta",
              "stream", "tract capital", "iren", "ada infrastructure", "xai"}
GENERIC_WORDS = {
    "wisconsin", "united", "american", "national", "general", "global", "holdings",
    "solutions", "technologies", "technology", "services", "company", "industrial",
    "development", "investment", "critical", "systems", "realty", "network",
    "communications", "platforms", "management", "data", "centers", "center",
    "trust", "group", "partners", "capital", "international", "corporation",
    "incorporated", "limited", "america", "north", "south", "east", "west",
    "digital", "cloud", "energy", "power", "electric", "infrastructure",
    "properties", "ventures", "enterprises", "industries", "products", "inc",
    "llc", "corp", "ltd", "co", "lp", "the", "of", "and", "dba", "ii", "iii",
    "facility", "gateway", "consulting", "signal",
}
MAX_ROWS_PER_TERM = 40  # broader hits are almost certainly a generic word

ADDR_SUFFIXES = {"st", "street", "ave", "avenue", "dr", "drive", "rd", "road", "ln",
                 "lane", "blvd", "boulevard", "ct", "court", "hwy", "highway", "pkwy",
                 "parkway", "way", "trail", "cty", "co", "county", "cnty", "sth", "cth",
                 "suite", "ste", "unit", "bldg", "corner", "of", "and", "at",
                 "intersection", "old"}
ADDR_DIRS = {"n", "s", "e", "w", "north", "south", "east", "west", "ne", "nw", "se", "sw"}

FACILITY_FIELDS = ["fid", "facility_name", "address", "city", "county", "facility_seq",
                   "tier", "match_terms", "lbl_facility", "discovered_at"]
INDEX_FIELDS = ["fid", "facility_name", "facility_seq", "tier", "permit_no", "permit_type",
                "app_received", "app_complete", "public_notice", "decision", "permit_status",
                "permit_writer", "doc_name", "object_id", "download_url", "status",
                "local_path", "existing_path", "size_bytes", "error", "indexed_at"]

TIER_RANK = {"A": 0, "B": 1, "C": 2}


class StopRun(Exception):
    """Raised when --limit / --facility-limit is reached."""


# --- HTTP ---------------------------------------------------------------------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class Warp:
    """Cookie-carrying urllib client with polite delay and retries."""

    def __init__(self, delay: float, timeout: int):
        cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self.op_nr = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect())
        for o in (self.op, self.op_nr):
            o.addheaders = [("User-Agent", UA)]
        self.delay, self.timeout = delay, timeout
        self.n_requests = 0

    def _open(self, opener, url: str, form=None, retries: int = 4, timeout: Optional[int] = None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        wait = 5.0
        for attempt in range(retries):
            try:
                self.n_requests += 1
                r = opener.open(urllib.request.Request(url, data=data), timeout=timeout or self.timeout)
                body = r.read()
                time.sleep(self.delay)
                return r.status, body, r.headers
            except urllib.error.HTTPError as e:
                if e.code in (301, 302):
                    time.sleep(self.delay)
                    return e.code, b"", e.headers
                if attempt == retries - 1:
                    raise
                logger.warning(f"HTTP {e.code} on {url[:90]} (attempt {attempt + 1}); retry in {wait:.0f}s")
            except Exception as e:  # noqa: BLE001
                if attempt == retries - 1:
                    raise
                logger.warning(f"{type(e).__name__}: {e} on {url[:90]} (attempt {attempt + 1}); retry in {wait:.0f}s")
            time.sleep(wait)
            wait *= 2
        raise RuntimeError("unreachable")

    def get(self, url: str) -> str:
        return self._open(self.op, url)[1].decode("utf-8", "replace")

    def post(self, url: str, form: Dict[str, str]) -> str:
        return self._open(self.op, url, form)[1].decode("utf-8", "replace")

    def post_noredirect(self, url: str, form: Dict[str, str]) -> Tuple[int, str, str]:
        status, body, headers = self._open(self.op_nr, url, form)
        return status, body.decode("utf-8", "replace"), headers.get("Location", "") if headers else ""

    def get_bytes(self, url: str, timeout: int) -> Tuple[bytes, str]:
        _, body, headers = self._open(self.op, url, timeout=timeout)
        return body, (headers.get("Content-Type", "") if headers else "")


def postback_form(form: Dict[str, str], target: str, argument: str = "") -> Dict[str, str]:
    f = dict(form)
    f["__EVENTTARGET"] = target
    f["__EVENTARGUMENT"] = argument
    return f


# --- search -------------------------------------------------------------------
def seq_from_location(location: str) -> str:
    m = re.search(r"id=(\d+)", location or "")
    return m.group(1) if m else ""


def parse_facility_header(page: str) -> Dict[str, str]:
    t = re.search(r'id="ctl00_ContentPlaceHolder1_lblTitle2"[^>]*>([^<]*)<', page)
    title = clean(t.group(1)) if t else ""
    m = re.match(r"(\d{9})\s+(.*)", title)
    fid, name = (m.group(1), m.group(2)) if m else ("", title)
    locs = [clean(x) for x in re.findall(r'id="ctl00_ContentPlaceHolder1_lblLocation\d"[^>]*>([^<]*)<', page)]
    return {"fid": fid, "facility_name": name.upper(), "address": locs[0] if locs else "",
            "city": locs[1] if len(locs) > 1 else "", "county": ""}


class Searcher:
    def __init__(self, client: Warp):
        self.client = client
        self.base_form: Dict[str, str] = {}
        self.cities: Set[str] = set()
        self.refresh()

    def refresh(self) -> None:
        page = self.client.get(SEARCH_URL)
        self.base_form = serialize_form(page)
        m = re.search(r'<select name="ctl00\$ContentPlaceHolder1\$ddlCity"[^>]*>(.*?)</select>', page, re.S)
        if m:
            self.cities = {htmllib.unescape(v) for v in re.findall(r'<option[^>]*value="([^"]*)"', m.group(1))}

    def search(self, fields: Dict[str, str]) -> Tuple[List[Dict[str, str]], Dict[str, str], str]:
        """Return (grid rows, results-page form, redirect seq)."""
        form = dict(self.base_form)
        form.update(fields)
        form = postback_form(form, P + "btnSearch")
        status, body, location = self.client.post_noredirect(SEARCH_URL, form)
        if status in (301, 302):
            return [], {}, seq_from_location(location)
        if "Please enter one or more search terms" in body:
            logger.warning(f"search rejected (no terms): {fields}")
        return parse_result_rows(body), serialize_form(body), ""

    def resolve_seq(self, row: Dict[str, str], results_form: Dict[str, str]) -> str:
        target = f"{P}gvResult${row['ctl']}$lnkSelect"
        status, _, location = self.client.post_noredirect(
            SEARCH_URL, postback_form(results_form, target))
        if status not in (301, 302):
            logger.warning(f"{row['fid']}: row select did not redirect (HTTP {status})")
            return ""
        return seq_from_location(location)


# --- discovery ----------------------------------------------------------------
class Facility(dict):
    """dict with fid, facility_name, address, city, county, facility_seq, tier, match_terms(set)."""


def better_tier(a: str, b: str) -> str:
    return a if TIER_RANK.get(a, 9) <= TIER_RANK.get(b, 9) else b


def derive_terms(term: str) -> List[str]:
    """Full term plus its first distinctive word (agency names differ from
    brand names, e.g. 'Windstream Holdings II' -> 'Windstream')."""
    out = [term]
    for w in term.split():
        wl = w.lower().strip("&-")
        if len(wl) >= 5 and wl not in GENERIC_WORDS and wl not in dc_targets.STOPWORDS:
            if wl != term.lower():
                out.append(w)
            break
    return out


def build_name_terms(extra_terms: Iterable[str]) -> List[Tuple[str, str, str]]:
    """[(term, kind, tier)] deduplicated, most specific kinds first."""
    terms: List[Tuple[str, str, str]] = []
    for t in DC_KEYWORD_TERMS:
        terms.append((t, "keyword", "A"))
    for t in WI_OPERATOR_TERMS:
        terms.append((t, "operator", "A"))
    national = {t.lower() for t in dc_targets.NATIONAL_ALIASES}
    for t in dc_targets.entity_terms("WI"):
        if t.lower() in national:  # already short brand names: use as-is
            tier = "B" if any(h in t.lower() for h in TIER_B_HINTS) else "A"
            terms.append((t, "operator", tier))
        else:
            # LBL-map legal names (mostly colo/telecom providers) -> tier B
            # unless also in the curated operator list (deduped above, tier A).
            for d in derive_terms(t):
                terms.append((d, "lbl_company", "B"))
    for t in extra_terms:
        terms.append((t, "cli", "A"))
    seen: Set[str] = set()
    out: List[Tuple[str, str, str]] = []
    for term, kind, tier in terms:
        key = term.strip().lower()
        if len(key) < 3 or key in SKIP_TERMS or key in seen:
            continue
        if kind == "lbl_company" and (key in dc_targets.STOPWORDS or key in GENERIC_WORDS):
            continue
        seen.add(key)
        out.append((term.strip(), kind, tier))
    return out


def address_term(addr) -> str:
    """Distinctive street token of an address ('5216-5222 90th St' -> '90th',
    'Corner of Highway 151 and Hemlock Road' -> 'Hemlock')."""
    if addr is None or (isinstance(addr, float)) or str(addr).strip().lower() in ("", "nan"):
        return ""
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", str(addr)).split()
    ordinal = re.compile(r"^\d+(st|nd|rd|th)$", re.I)
    while words and re.match(r"^[NSEW]?\d[\dA-Za-z]*$", words[0]) and not ordinal.match(words[0]):
        words.pop(0)
    while words and words[0].lower() in ADDR_DIRS:
        words.pop(0)
    for w in words:
        if w.lower() in ADDR_SUFFIXES or w.lower() in ADDR_DIRS or w.isdigit():
            continue
        if ordinal.match(w) or len(w) >= 3:
            return w
    return ""


def lbl_address_targets() -> List[Tuple[str, str, str]]:
    """[(street term, CITY, lbl facility label)] for WI rows of the LBL map."""
    df = dc_targets.load_combined()
    sub = df[df["state"].astype(str).str.upper() == "WI"]
    out, seen = [], set()
    for _, r in sub.iterrows():
        city = str(r.get("city", "") or "").strip().upper()
        term = address_term(r.get("address", "") or "")
        if not term or not city or city == "NAN":
            continue
        key = (term.lower(), city)
        if key in seen:
            continue
        seen.add(key)
        label = f"{r.get('facility_name', '')} / {r.get('company', '')}"
        out.append((term, city, label))
    return out


def discover(searcher: Searcher, client: Warp, sics: List[str], naics: List[str],
             name_terms: List[Tuple[str, str, str]], addr_targets: List[Tuple[str, str, str]],
             extra_fids: List[str]) -> Dict[str, Facility]:
    facilities: Dict[str, Facility] = {}

    def add_rows(rows: List[Dict[str, str]], results_form: Dict[str, str], redirect_seq: str,
                 label: str, tier, lbl: str = "", keep=lambda r: True) -> int:
        """Merge search hits into `facilities`; `tier` is a str or f(row)->str."""
        n = 0
        if redirect_seq:
            page = client.get(FACILITY_URL.format(seq=redirect_seq))
            hdr = parse_facility_header(page)
            if not hdr["fid"]:
                logger.warning(f"{label}: redirect to seq {redirect_seq} but no FID parsed")
                return 0
            rows = [dict(hdr, facility_seq=redirect_seq)]
        for r in rows:
            if not keep(r):
                continue
            fid = r["fid"]
            row_tier = tier(r) if callable(tier) else tier
            fac = facilities.get(fid)
            if fac is None:
                seq = r.get("facility_seq") or searcher.resolve_seq(r, results_form)
                fac = Facility(fid=fid, facility_name=r["facility_name"], address=r["address"],
                               city=r["city"], county=r.get("county", ""), facility_seq=seq,
                               tier=row_tier, match_terms=set(), lbl_facility=lbl)
                facilities[fid] = fac
                n += 1
            else:
                fac["tier"] = better_tier(fac["tier"], row_tier)
                if not fac["county"] and r.get("county"):
                    fac["county"] = r["county"]
                if lbl and not fac["lbl_facility"]:
                    fac["lbl_facility"] = lbl
            fac["match_terms"].add(label)
        return n

    for sic in sics:
        rows, rform, seq = searcher.search({P + "txtSic": sic, P + "grpSic": "rdbSic3"})
        n = add_rows(rows, rform, seq, f"sic:{sic}", "A")
        logger.info(f"SIC {sic}: {len(rows) or (1 if seq else 0)} facilities ({n} new)")
    for code in naics:
        rows, rform, seq = searcher.search({P + "txtNaics": code, P + "grpNaics": "rdbNaics3"})
        n = add_rows(rows, rform, seq, f"naics:{code}", "A")
        logger.info(f"NAICS {code}: {len(rows) or (1 if seq else 0)} facilities ({n} new)")

    for i, (term, kind, tier) in enumerate(name_terms, 1):
        rows, rform, seq = searcher.search({P + "txtFacName": term.upper(), P + "grpName": "rdbFacName2"})
        total = len(rows) or (1 if seq else 0)
        if total == 0:
            continue
        # CONTAINS is a raw substring match ("VANTAGE" hits "ADVANTAGE ..."),
        # so require the term as whole word(s) in the facility name.
        word_re = re.compile(r"(?<![A-Z0-9])" + re.escape(term.upper()) + r"(?![A-Z0-9])")
        if kind == "keyword":
            keep = lambda r: bool(DC_NAME_RE.search(r["facility_name"]))  # noqa: E731
        elif len(rows) > MAX_ROWS_PER_TERM:
            logger.warning(f"[{i}/{len(name_terms)}] {term!r}: {len(rows)} hits - too generic, keeping DC-named rows only")
            keep = lambda r: bool(DC_NAME_RE.search(r["facility_name"]))  # noqa: E731
        else:
            keep = lambda r: bool(word_re.search(r["facility_name"].upper()))  # noqa: E731
        n = add_rows(rows, rform, seq, f"name:{term}", tier, keep=keep)
        logger.info(f"[{i}/{len(name_terms)}] name~{term!r} ({kind}, tier {tier}): {total} hits, {n} new facilities")

    for term, city, lbl in addr_targets:
        if city not in searcher.cities:
            logger.debug(f"address {term!r}: city {city!r} not in WARP city list; skipped")
            continue
        rows, rform, seq = searcher.search({P + "txtAddress": term, P + "grpAddress": "rdbAddress2",
                                            P + "ddlCity": city})
        total = len(rows) or (1 if seq else 0)
        if total == 0:
            continue
        if len(rows) > MAX_ROWS_PER_TERM:
            logger.warning(f"address {term!r} + {city}: {len(rows)} hits - skipped as too generic")
            continue
        # DC-looking names rank tier A; everything else is tier C (address-only:
        # indexed for review, not downloaded unless --tiers includes C).
        n = add_rows(rows, rform, seq, f"address:{term}|{city}",
                     tier=lambda r: "A" if DC_NAME_RE.search(r["facility_name"]) else "C", lbl=lbl)
        logger.info(f"address~{term!r} + {city} [{lbl[:40]}]: {total} hits, {n} new facilities")

    for fid in extra_fids:
        rows, rform, seq = searcher.search({P + "txtFid": fid, P + "grpFid": "rdbFid3"})
        n = add_rows(rows, rform, seq, f"fid:{fid}", "A")
        logger.info(f"FID {fid}: {len(rows) or (1 if seq else 0)} facilities ({n} new)")

    return facilities


# --- existing documents ---------------------------------------------------------
def load_existing_object_ids() -> Dict[str, str]:
    """object_id -> path for PDFs already fetched by the earlier WI scrapers."""
    found: Dict[str, str] = {}
    for root in EXISTING_ROOTS:
        pdf_dir = root / "pdfs"
        if pdf_dir.is_dir():
            for p in pdf_dir.glob("*.pdf"):
                m = re.match(r"\d+_(\d+)_", p.name) or re.search(r"dwnld_(\d+)\.pdf$", p.name)
                if m:
                    found.setdefault(m.group(1), str(p))
        log = root / "wi_dnr_download_log.csv"
        if log.exists():
            with open(log, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r.get("status") == "downloaded" and r.get("object_id"):
                        path = pdf_dir / r.get("filename", "")
                        if path.exists():
                            found.setdefault(r["object_id"], str(path))
    return found


# --- CSV helpers ----------------------------------------------------------------
class RowWriter:
    def __init__(self, path: Path, fields: List[str]):
        self.path, self.fields = path, fields
        new = not path.exists() or path.stat().st_size == 0
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fieldnames=fields, extrasaction="ignore")
        if new:
            self.w.writeheader()
            self.f.flush()

    def write(self, row: Dict[str, str]) -> None:
        self.w.writerow(row)
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:180]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- enumeration + download -----------------------------------------------------
class Run:
    def __init__(self, client: Warp, args, existing: Dict[str, str]):
        self.client, self.args, self.existing = client, args, existing
        self.index = RowWriter(INDEX_CSV, INDEX_FIELDS)
        prior = read_csv(INDEX_CSV)
        self.done_keys: Set[Tuple[str, str, str]] = set()
        self.done_facilities: Set[str] = set()
        self.local_by_oid: Dict[str, str] = {}
        # The index is an append-only log: the LAST row per (fid, permit_no,
        # doc_name) wins. error / not_downloaded rows are retried on resume.
        for r in prior:
            if r["status"] == "facility_enumerated":
                self.done_facilities.add(r["fid"])
            elif r["status"] not in ("error", "not_downloaded"):
                self.done_keys.add((r["fid"], r["permit_no"], r["doc_name"]))
            if r["status"] == "downloaded" and r["local_path"]:
                self.local_by_oid[r["object_id"]] = r["local_path"]
        self.stats = {"downloaded": 0, "skipped_existing": 0, "dup_object_id": 0, "error": 0,
                      "not_downloaded": 0, "indexed": 0, "facilities": 0}
        logger.info(f"resume: {len(self.done_facilities)} facilities complete, "
                    f"{len(self.done_keys)} docs indexed, {len(self.local_by_oid)} PDFs on disk")

    def download(self, oid: str, fid: str, doc_name: str) -> Tuple[str, str, int, str]:
        name = doc_name or f"{fid}_{oid}.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = PDF_DIR / sanitize(f"{fid}_{oid}_{name}")
        if dest.exists() and dest.stat().st_size > 0:
            return "downloaded", str(dest), dest.stat().st_size, ""
        try:
            data, ctype = self.client.get_bytes(DOWNLOAD_URL.format(oid=oid), timeout=self.args.timeout * 5)
            if not data.startswith(b"%PDF"):
                return "error", "", len(data), f"not a PDF ({len(data)} bytes, {ctype})"
            dest.write_bytes(data)
            return "downloaded", str(dest), len(data), ""
        except Exception as e:  # noqa: BLE001
            return "error", "", 0, f"{type(e).__name__}: {e}"

    def process_facility(self, fac: Facility) -> None:
        fid = fac["fid"]
        if fid in self.done_facilities:
            logger.debug(f"{fid} {fac['facility_name']}: already enumerated, skipping")
            return
        if not fac["facility_seq"]:
            logger.warning(f"{fid} {fac['facility_name']}: no facility seq; skipping")
            return
        url = FACILITY_URL.format(seq=fac["facility_seq"])
        page = self.client.get(url)
        form = serialize_form(page)
        chk = P + "chkInactive"
        if f'name="{chk}"' in page:
            form[chk] = "on"
            page = self.client.post(url, postback_form(form, chk))
            form = dict(serialize_form(page), **{chk: "on"})
        permits = parse_permit_rows(page)
        n_docs = 0
        for pm in permits:
            cells = pm["cells"] + [""] * 8
            permit = {"permit_no": cells[0], "permit_type": cells[1], "app_received": cells[2],
                      "app_complete": cells[3], "public_notice": cells[4], "decision": cells[5],
                      "permit_status": cells[6], "permit_writer": cells[7]}
            p2 = self.client.post(url, postback_form(form, P + "gvPermits", f"Select${pm['select_idx']}"))
            form2 = dict(serialize_form(p2), **({chk: "on"} if chk in form else {}))
            docs = parse_doc_rows(p2)
            if not docs:
                self.emit(fac, permit, {"doc_name": "", "object_id": ""}, "no_documents")
            for d in docs:
                key = (fid, permit["permit_no"], d["doc_name"])
                if key in self.done_keys:
                    continue
                p3 = self.client.post(url, postback_form(form2, P + "gvDocuments", f"Select${d['select_idx']}"))
                m = re.search(r"AM_DownloadObject\.aspx\?id=(\d+)", p3)
                oid = m.group(1) if m else ""
                self.handle_doc(fac, permit, d, oid)
                n_docs += 1
        self.emit(fac, {"permit_no": "", "permit_type": ""}, {"doc_name": "", "object_id": ""},
                  "facility_enumerated")
        self.done_facilities.add(fid)
        self.stats["facilities"] += 1
        logger.info(f"{fid} {fac['facility_name']} (tier {fac['tier']}): {len(permits)} permits, "
                    f"{n_docs} docs indexed this run")

    def handle_doc(self, fac: Facility, permit: Dict[str, str], d: Dict[str, str], oid: str) -> None:
        if not oid:
            self.emit(fac, permit, d, "error", error="no object id in postback response")
            return
        if oid in self.existing:
            self.emit(fac, permit, d, "skipped_existing", oid=oid, existing_path=self.existing[oid])
            return
        if oid in self.local_by_oid:
            self.emit(fac, permit, d, "dup_object_id", oid=oid, local_path=self.local_by_oid[oid])
            return
        if self.args.no_download or fac["tier"] not in self.args.tiers:
            self.emit(fac, permit, d, "not_downloaded", oid=oid)
            return
        if self.args.limit is not None and self.stats["downloaded"] >= self.args.limit:
            self.emit(fac, permit, d, "not_downloaded", oid=oid, error="limit reached")
            raise StopRun(f"--limit {self.args.limit} reached")
        status, path, size, err = self.download(oid, fac["fid"], d["doc_name"])
        if status == "downloaded":
            self.local_by_oid[oid] = path
            logger.info(f"  {Path(path).name} ({size:,} B)")
        else:
            logger.error(f"  {d['doc_name']}: {err}")
        self.emit(fac, permit, d, status, oid=oid, local_path=path, size=size, error=err)

    def emit(self, fac: Facility, permit: Dict[str, str], d: Dict[str, str], status: str,
             oid: str = "", local_path: str = "", existing_path: str = "", size: int = 0,
             error: str = "") -> None:
        row = {"fid": fac["fid"], "facility_name": fac["facility_name"],
               "facility_seq": fac["facility_seq"], "tier": fac["tier"]}
        row.update({k: permit.get(k, "") for k in ("permit_no", "permit_type", "app_received",
                                                   "app_complete", "public_notice", "decision",
                                                   "permit_status", "permit_writer")})
        row.update({"doc_name": d.get("doc_name", ""), "object_id": oid,
                    "download_url": DOWNLOAD_URL.format(oid=oid) if oid else "",
                    "status": status, "local_path": local_path, "existing_path": existing_path,
                    "size_bytes": size or "", "error": error, "indexed_at": now()})
        self.index.write(row)
        if status in self.stats:
            self.stats[status] += 1
        if status not in ("facility_enumerated", "no_documents"):
            self.stats["indexed"] += 1
            self.done_keys.add((fac["fid"], permit.get("permit_no", ""), d.get("doc_name", "")))


# --- main -----------------------------------------------------------------------
def load_or_discover(args, client: Warp) -> List[Facility]:
    if FACILITIES_CSV.exists() and not args.rediscover:
        rows = read_csv(FACILITIES_CSV)
        facs = [Facility(r, match_terms=set(filter(None, r["match_terms"].split(";")))) for r in rows]
        logger.info(f"loaded {len(facs)} facilities from {FACILITIES_CSV} (use --rediscover to refresh)")
        return facs

    searcher = Searcher(client)
    name_terms = [] if args.no_name_search else build_name_terms(
        [t.strip() for t in (args.terms or "").split(",") if t.strip()])
    addr_targets = [] if args.no_address_search else lbl_address_targets()
    sics = [s.strip() for s in args.sics.split(",") if s.strip()]
    naics = [s.strip() for s in args.naics.split(",") if s.strip()]
    extra_fids = [s.strip() for s in (args.fids or "").split(",") if s.strip()]
    logger.info(f"discovery: {len(sics)} SIC, {len(naics)} NAICS, {len(name_terms)} name terms, "
                f"{len(addr_targets)} LBL address targets, {len(extra_fids)} FIDs")
    found = discover(searcher, client, sics, naics, name_terms, addr_targets, extra_fids)
    facs = sorted(found.values(), key=lambda f: (TIER_RANK[f["tier"]], f["facility_name"]))
    prior = {r["fid"] for r in read_csv(FACILITIES_CSV)} if FACILITIES_CSV.exists() else set()
    w = RowWriter(FACILITIES_CSV, FACILITY_FIELDS)
    for f in facs:
        if f["fid"] in prior:
            continue
        w.write(dict(f, match_terms=";".join(sorted(f["match_terms"])), discovered_at=now()))
    w.close()
    logger.success(f"{len(facs)} facilities discovered "
                   f"(A={sum(f['tier'] == 'A' for f in facs)}, B={sum(f['tier'] == 'B' for f in facs)}, "
                   f"C={sum(f['tier'] == 'C' for f in facs)}) -> {FACILITIES_CSV}")
    return facs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="Stop after downloading N new PDFs (tests)")
    ap.add_argument("--facility-limit", type=int, default=None, help="Process at most N facilities")
    ap.add_argument("--tiers", default="A,B", help="Facility tiers to download (default A,B; C = address-only hits)")
    ap.add_argument("--sics", default=",".join(DEFAULT_SICS), help="Exact SIC codes to sweep")
    ap.add_argument("--naics", default=",".join(DEFAULT_NAICS), help="Exact NAICS codes to sweep")
    ap.add_argument("--terms", default="", help="Extra facility-name CONTAINS terms (comma-separated)")
    ap.add_argument("--fids", default="", help="Extra FIDs to include (comma-separated)")
    ap.add_argument("--no-name-search", action="store_true")
    ap.add_argument("--no-address-search", action="store_true")
    ap.add_argument("--rediscover", action="store_true", help="Re-run facility discovery even if the facilities CSV exists")
    ap.add_argument("--no-download", action="store_true", help="Index documents only")
    ap.add_argument("--delay", type=float, default=0.5, help="Seconds between requests")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()
    args.tiers = {t.strip().upper() for t in args.tiers.split(",") if t.strip()}

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(exist_ok=True)
    logger.add(OUTPUT_DIR / "download_wi_dnr_dc_permits.log", rotation="20 MB")
    logger.info(f"args: {vars(args)}")

    client = Warp(delay=args.delay, timeout=args.timeout)
    existing = load_existing_object_ids()
    logger.info(f"{len(existing)} object ids already on disk under {[str(r) for r in EXISTING_ROOTS]}")

    facilities = load_or_discover(args, client)
    todo = [f for f in facilities if f["tier"] in args.tiers]
    if args.facility_limit is not None:
        todo = todo[: args.facility_limit]
    logger.info(f"{len(todo)} facilities to enumerate (tiers {sorted(args.tiers)})")

    run = Run(client, args, existing)
    try:
        for fac in todo:
            try:
                run.process_facility(fac)
            except StopRun:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"{fac['fid']} {fac['facility_name']}: {type(e).__name__}: {e}")
    except StopRun as e:
        logger.warning(str(e))
    finally:
        run.index.close()

    s = run.stats
    logger.success(
        f"done: {s['facilities']} facilities enumerated, {s['indexed']} doc rows indexed, "
        f"{s['downloaded']} downloaded, {s['skipped_existing']} already in earlier WI dirs, "
        f"{s['dup_object_id']} duplicate objects, {s['not_downloaded']} not downloaded, "
        f"{s['error']} errors; {client.n_requests} HTTP requests -> {INDEX_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
