#!/usr/bin/env python3
"""Download Ohio EPA air-permit documents for data centers.

Portal: Ohio EPA eDocument Search
        https://edocpub.epa.ohio.gov/publicportal/edochome.aspx

The portal is a classic ASP.NET WebForms page; this script drives it with
plain ``requests`` (no Selenium needed):

  GET  edochome.aspx                         -> __VIEWSTATE / __EVENTVALIDATION
  POST edochome.aspx  (btnSearch)            -> results grid, 10 rows/page
       keyword fields (all "Equal", joined "Or"):
         ctl00$search$KeywordPanel1$txtValue_-1_1_106_1   Entity Name
                (prefix match; ``*`` is a wildcard, so ``*COLOGIX*`` = substring)
         ctl00$search$KeywordPanel1$txtValue_-1_1_111_1   Secondary ID (= 10-digit
                air facility ID, e.g. 0145000544)
         ctl00$search$KeywordPanel1$ddlValue_-1_1_109_1   Program (AIR PERMIT)
         ctl00$search$txtFullText                         full-text (weak: matches
                metadata, not OCR'd bodies)
  POST __EVENTTARGET=ctl00$results$DocHitList$DocHitList_CurrentPageSize
       (+ ...CurrentPageSize=600)            -> 600 rows/page
  POST __EVENTTARGET=ctl00$results$DocHitList$lnkNextPage
  GET  ViewDocument.aspx?docid=<n>           -> the PDF itself (no session needed)

Each grid row's title is
  ENTITY - DOC TYPE - M/D/YYYY - PROGRAM - COUNTY - FACILITY_ID - [SUBTYPE - - PKG] - DOCID
which is parsed into the index columns.

Pipeline
--------
1. discover : entity-name searches (prefix + ``*substring*``) for every
              data-center operator / code-name term, plus optional full-text
              terms.  Results are grouped by facility ID.  Terms that return a
              flood of unrelated facilities are treated as generic and kept only
              where the entity name looks like a data center.
2. pull     : one Secondary-ID search per matched facility so that *every*
              AIR PERMIT document for the facility is captured, whatever entity
              name it was filed under.
3. download : ViewDocument.aspx?docid=N -> pdfs/<docid>_<title>.pdf.  Docids
              already present in the earlier program-wide sweep
              (data/raw/ohio_epa_air_permits) are indexed with
              already_in_sweep=1 instead of being re-downloaded.

Resumable: discovery is cached in discovery.json, the index CSV is appended
row by row and re-read on start (failed downloads are retried).

Output layout
-------------
  <RAW_DATA_DIR>/oh_epa_dc_permits/
    pdfs/<docid>_<title>.pdf
    oh_epa_dc_permits_index.csv           one row per document
    oh_epa_dc_facilities.csv              one row per matched facility
    oh_epa_dc_candidates.csv              facilities seen only via generic /
                                          full-text hits (NOT downloaded)
    discovery.json                        per-term search cache
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import requests
from bs4 import BeautifulSoup
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

import dc_targets  # noqa: E402

BASE = "https://edocpub.epa.ohio.gov/publicportal/"
SEARCH_URL = BASE + "edochome.aspx"
VIEW_URL = BASE + "ViewDocument.aspx?docid={docid}"
RESULTS_TABLE_ID = "ctl00_results_DocHitList_tblDocuments"
NEXT_LINK_ID = "ctl00_results_DocHitList_lnkNextPage"
PAGE_SIZE_FIELD = "ctl00$results$DocHitList$DocHitList_CurrentPageSize"
PAGE_NUM_FIELD = "ctl00$results$DocHitList$DocHitList_CurrentPageNum"
NEXT_TARGET = "ctl00$results$DocHitList$lnkNextPage"
NO_DOCS_TEXT = "There are no documents to display."

OUTPUT_DIR = RAW_DATA_DIR / "oh_epa_dc_permits"
SWEEP_DIR = RAW_DATA_DIR / "ohio_epa_air_permits"
SWEEP_INDEX = SWEEP_DIR / "ohio_epa_air_permits_index.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
}

INDEX_FIELDS = [
    "facility_id", "entity_name", "doc_type", "doc_date", "program", "county",
    "subtype", "permit_number", "docid", "title", "document_url",
    "match_sources", "status", "already_in_sweep", "sweep_file_present", "local_path",
]
FACILITY_FIELDS = ["facility_id", "entity_names", "county", "n_docs", "match_sources", "is_dc_name"]
CANDIDATE_FIELDS = ["facility_id", "entity_names", "county", "n_hits", "match_sources"]

# Ohio-specific operator / code-name terms.  Plain terms are prefix searches;
# ``*term*`` terms are substring searches.  Data-center campuses in Ohio are
# frequently permitted under shell LLCs (Sidecat = Meta, Maguro / Bistrozzi /
# Table Mountain = Google, Montauk / Turnstile = Meta & Microsoft-era New
# Albany, Project Granite = Amazon Dublin ...), so code names are listed too.
OHIO_TERMS: List[str] = [
    "CMH*", "*CMH0*", "*CMH1*", "*CMH2*",              # Amazon campus codes (CMH0xx ...)
    "VADATA", "*VADATA*", "AMAZON", "*AMAZON*", "AWS ",
    "MAGURO", "*MAGURO*", "GOOGLE", "*GOOGLE*", "ALPHABET",
    "MICROSOFT", "*MICROSOFT*", "AZURE",
    "SIDECAT", "*SIDECAT*", "META PLATFORMS", "*META PLATFORMS*", "FACEBOOK", "*FACEBOOK*",
    "QTS", "*QTS *", "QUALITY TECH",
    "COLOGIX", "*COLOGIX*", "CYRUSONE", "*CYRUSONE*", "CYRUS ONE",
    "TURNSTILE", "*TURNSTILE*", "MONTAUK", "*MONTAUK*",
    "BISTROZZI", "*BISTROZZI*", "PROJECT GRANITE", "*PROJECT GRANITE*",
    "TABLE MOUNTAIN", "*TABLE MOUNTAIN*", "LAVENDER", "*LAVENDER*",
    "SOCRATES", "*SOCRATES*", "WILLIAMS SOCRATES",
    "ALIGNED", "*ALIGNED DATA*", "*ALIGNED CMH*", "*ALIGNED NEO*",
    "VANTAGE", "*VANTAGE DATA*", "EDGECONNEX", "*EDGECONNEX*", "EDGE CONNEX",
    "STACK INFRA", "*STACK INFRA*", "EXPEDIENT", "*EXPEDIENT*",
    "INVOLTA", "*INVOLTA*", "H5 DATA", "*H5 DATA*", "DATABANK", "*DATABANK*",
    "T5 DATA", "*T5 DATA*", "IRON MOUNTAIN", "*IRON MOUNTAIN*",
    "FLEXENTIAL", "*FLEXENTIAL*", "DARTPOINTS", "*DARTPOINTS*",
    "5C DATA", "*5C DATA*", "EDGED", "*EDGED ENERGY*", "KARIS", "*KARIS*",
    "RACKHOUSE", "*RACKHOUSE*", "PROLOGIS", "*PROLOGIS*", "THOR EQUITIES",
    "MARA HOLDINGS", "COMPASS MINING", "COMPASS DATACENTER", "*COMPASS DATACENTER*",
    "ORACLE", "OPENAI", "*OPENAI*", "STARGATE", "*STARGATE*", "SOFTBANK",
    "CITI MIDWEST", "*CITI MIDWEST*", "ARK DATA", "*ARK DATA*",
    "CRUSOE", "*CRUSOE*", "COREWEAVE", "*COREWEAVE*", "EQUINIX", "*EQUINIX*",
    "DIGITAL REALTY", "*DIGITAL REALTY*", "SABEY", "YONDR", "*YONDR*", "CLOUDHQ", "*CLOUDHQ*",
    "NTT GLOBAL", "NTT DATA", "CORESITE", "*CORESITE*", "SKYBOX", "TRACT ",
    "DATA CENTER", "*DATA CENTER*", "DATACENTER", "*DATACENTER*", "*DATA CENTRE*",
    "*HYPERSCALE*", "*COLOCATION*", "*SERVER FARM*", "*CLOUD COMPUT*", "*HOSTING*",
    "*BITCOIN*", "*CRYPTO*",
]

# Terms from dc_targets that are too generic for a prefix search of a
# state-wide permit database.
SKIP_TERMS = {
    "SWITCH", "DESIGN LLC", "HOT SITE", "ENET", "PCM INC", "GREEN HARBOR", "CENTERSQUARE",
    "ROCK OHIO CAESARS LLC", "NTT", "XAI", "IREN", "MARA", "COMPASS", "TRACT CAPITAL",
    "LAMBDA LABS", "HUT 8", "STREAM DATA", "STRATEGIC DATASPHERE LLC", "SAILION",
}

# Entity names that look like data centers even when found via a generic term.
DC_NAME_PATTERN = re.compile(
    r"data ?cent|datacenter|colocat|hyperscale|server farm|cloud comput|hosting|\bCMH ?\d|"
    r"vadata|amazon|microsoft|google|maguro|sidecat|meta platforms|facebook|\bqts\b|"
    r"cologix|cyrusone|cyrus one|turnstile|montauk|bistrozzi|project granite|"
    r"table mountain|lavender|socrates|\baligned|\bvantage|edgeconnex|edge connex|stack infra|"
    r"expedient|involta|h5 data|databank|t5 data|iron mountain|flexential|dartpoints|"
    r"5c data|\bedged|\bkaris|rackhouse|prologis|thor equit|mara holdings|compass (data|mining)|"
    r"\boracle|openai|stargate|softbank|citi midwest|ark data|crusoe|coreweave|equinix|"
    r"digital realty|sabey|yondr|cloudhq|coresite|bitcoin|crypto",
    re.IGNORECASE,
)

DOC_TYPE_NAMES = {
    "Adjudication", "Appeal", "Assessment", "Bilateral Compliance Agreement (BCA)",
    "Compliance Notification", "Cost Recovery", "Covenant",
    "Director's Final Findings and Orders (DFFO)", "Director's Authorization",
    "General Correspondence", "Incident Report", "Inspection or Compliance Review",
    "Investigation Report", "Judicial Order", "Lab Certification", "Loans",
    "Monitoring or Sampling", "Non-permit Related Exemptions", "Non-permit Related Variance",
    "Notice of Violation (NOV)", "Ohio EPA Monitoring", "Permit", "Plan",
    "Professional Certification", "Referral", "Registration", "Remediation Reports",
    "Remediation Response", "Report", "Rescission",
    "Resolution of Violation (ROV) or Return to Compliance (RTC)", "Settlement Cost Recovery",
    "Technical Assistance", "Trade Secret Correspondence", "UIC Monitoring Reports",
    "Verified Complaint", "Warning Letter",
}

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
FACILITY_ID_RE = re.compile(r"^\d{10}$")
PERMIT_NO_RE = re.compile(r"^[A-Z]\d{7}$")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def clean_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return re.sub(r"\s+", " ", name).strip()


def parse_title(title: str, docid: str) -> Dict[str, str]:
    """Split 'ENTITY - DOCTYPE - DATE - PROGRAM - COUNTY - FID - [SUBTYPE - - PKG] - DOCID'."""
    parts = [p.strip() for p in title.split(" - ")]
    out = {k: "" for k in ("entity_name", "doc_type", "doc_date", "program", "county",
                           "facility_id", "subtype", "permit_number")}
    date_idx = next((i for i, p in enumerate(parts) if DATE_RE.match(p)), None)
    if date_idx is None:
        out["entity_name"] = parts[0] if parts else title
        return out
    pre = parts[:date_idx]
    split_at = 1
    for i in range(len(pre) - 1, 0, -1):
        if pre[i] in DOC_TYPE_NAMES:
            split_at = i
            break
    out["entity_name"] = " - ".join(pre[:split_at])
    out["doc_type"] = " - ".join(pre[split_at:])
    out["doc_date"] = parts[date_idx]
    tail = parts[date_idx + 1:]
    if tail:
        out["program"] = tail[0]
    if len(tail) > 1:
        out["county"] = tail[1]
    if len(tail) > 2 and FACILITY_ID_RE.match(tail[2]):
        out["facility_id"] = tail[2]
        rest = tail[3:]
    else:
        rest = tail[2:]
    rest = [r for r in rest if r and r != docid]
    for r in rest:
        if PERMIT_NO_RE.match(r):
            out["permit_number"] = r
        elif not out["subtype"]:
            out["subtype"] = r
    return out


def iso_date(mdy: str) -> str:
    try:
        return datetime.strptime(mdy, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


# --------------------------------------------------------------------------- #
# portal client
# --------------------------------------------------------------------------- #
class Portal:
    def __init__(self, timeout: int, delay: float, page_size: int = 600):
        self.timeout = timeout
        self.delay = delay
        self.page_size = page_size
        self.session = self._new_session()

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        return s

    @staticmethod
    def _hidden(soup: BeautifulSoup) -> Dict[str, str]:
        return {i["name"]: i.get("value", "")
                for i in soup.find_all("input", type="hidden") if i.get("name")}

    @staticmethod
    def _search_fields(entity: str = "", secondary: str = "", fulltext: str = "",
                       program: str = "AIR PERMIT", county: str = "") -> Dict[str, str]:
        kp = "ctl00$search$KeywordPanel1$"
        f = {"ctl00$search$ddlDocType": "-1", kp + "txtFrom": "", kp + "txtTo": "",
             "ctl00$search$txtFullText": fulltext}
        for kw, val, is_select in (("111", secondary, False), ("106", entity, False),
                                   ("104", county, True), ("109", program, True), ("121", "", False)):
            f[f"{kp}ddlOp_-1_1_{kw}_1"] = "Equal"
            f[f"{kp}{'ddlValue' if is_select else 'txtValue'}_-1_1_{kw}_1"] = val
            f[f"{kp}ddlConn_-1_1_{kw}_1"] = "Or"
        return f

    @staticmethod
    def _rows(html: str) -> List[Dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find(id=RESULTS_TABLE_ID)
        rows: List[Dict[str, str]] = []
        if not table:
            return rows
        for tr in table.find_all("tr"):
            a = tr.find("a", onclick=re.compile(r"ViewDocument\.aspx\?docid=\d+", re.I))
            if not a:
                continue
            m = re.search(r"docid=(\d+)", a["onclick"], re.I)
            docid = m.group(1)
            title = a.get_text(" ", strip=True)
            row = {"docid": docid, "title": title, "document_url": VIEW_URL.format(docid=docid)}
            row.update(parse_title(title, docid))
            rows.append(row)
        return rows

    def _post(self, data: Dict[str, str]) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = self.session.post(SEARCH_URL, data=data, timeout=self.timeout,
                                      headers={"Referer": SEARCH_URL})
                r.raise_for_status()
                return r.text
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(f"POST failed (attempt {attempt + 1}): {exc}")
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"portal POST failed: {last_exc}")

    def search(self, max_pages: int = 20, **criteria) -> List[Dict[str, str]]:
        """Run one search and page through the grid; returns unique rows."""
        fields = self._search_fields(**criteria)
        r = self.session.get(SEARCH_URL, timeout=self.timeout)
        r.raise_for_status()
        data = self._hidden(BeautifulSoup(r.text, "html.parser"))
        data.update(fields)
        data.update({"__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
                     "ctl00$search$btnSearch": "Search"})
        html = self._post(data)
        if NO_DOCS_TEXT in html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        if not soup.find(id=RESULTS_TABLE_ID):
            raise RuntimeError("no results table and no 'no documents' message")

        # bump page size
        data = self._hidden(soup)
        data.update(fields)
        data.update({"__EVENTTARGET": PAGE_SIZE_FIELD, "__EVENTARGUMENT": "", "__LASTFOCUS": "",
                     PAGE_SIZE_FIELD: str(self.page_size), PAGE_NUM_FIELD: "1"})
        html = self._post(data)
        soup = BeautifulSoup(html, "html.parser")
        rows = self._rows(html)
        seen = {x["docid"] for x in rows}
        page = 1
        while page < max_pages:
            nxt = soup.find(id=NEXT_LINK_ID)
            if not nxt or not nxt.get("href"):
                break
            data = self._hidden(soup)
            data.update(fields)
            data.update({"__EVENTTARGET": NEXT_TARGET, "__EVENTARGUMENT": "", "__LASTFOCUS": "",
                         PAGE_SIZE_FIELD: str(self.page_size), PAGE_NUM_FIELD: str(page)})
            time.sleep(self.delay)
            html = self._post(data)
            soup = BeautifulSoup(html, "html.parser")
            page_rows = self._rows(html)
            fresh = [x for x in page_rows if x["docid"] not in seen]
            if not fresh:
                break
            seen.update(x["docid"] for x in fresh)
            rows.extend(fresh)
            page += 1
        return rows

    def download(self, docid: str, dest: Path) -> bool:
        url = VIEW_URL.format(docid=docid)
        tmp = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(3):
            try:
                with self.session.get(url, stream=True, timeout=(30, self.timeout * 5),
                                      headers={"Referer": SEARCH_URL,
                                               "Accept": "application/pdf,*/*;q=0.8"}) as r:
                    r.raise_for_status()
                    with tmp.open("wb") as fh:
                        first = b""
                        for chunk in r.iter_content(chunk_size=1 << 16):
                            if not chunk:
                                continue
                            if not first:
                                first = chunk[:8]
                                if not first.startswith(b"%PDF"):
                                    logger.warning(f"docid {docid}: not a PDF "
                                                   f"({r.headers.get('Content-Type')}); skipping")
                                    return False
                            fh.write(chunk)
                if tmp.stat().st_size < 100:
                    raise RuntimeError("empty response")
                tmp.replace(dest)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"docid {docid}: download failed (attempt {attempt + 1}): {exc}")
                time.sleep(3 * (attempt + 1))
            finally:
                if tmp.exists():
                    tmp.unlink()
        return False


# --------------------------------------------------------------------------- #
# sweep cross-reference
# --------------------------------------------------------------------------- #
def load_sweep(sweep_index: Path, sweep_dir: Path) -> Dict[str, Optional[Path]]:
    """docid -> local sweep path (None when the index row exists but the file was trimmed)."""
    out: Dict[str, Optional[Path]] = {}
    if sweep_dir.exists():
        for p in sweep_dir.iterdir():
            m = re.match(r"(\d+)_", p.name)
            if m and p.suffix.lower() == ".pdf":
                out[m.group(1)] = p
    if sweep_index.exists():
        with sweep_index.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                d = (row.get("docid") or "").strip()
                if d.isdigit() and row.get("status") == "downloaded" and d not in out:
                    out[d] = None
    logger.info(f"Sweep cross-reference: {len(out)} docids "
                f"({sum(1 for p in out.values() if p is not None)} with a file on disk)")
    return out


# --------------------------------------------------------------------------- #
# index writer
# --------------------------------------------------------------------------- #
class IndexWriter:
    def __init__(self, path: Path, fields: List[str]):
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists() or path.stat().st_size == 0
        self._fh = path.open("a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=fields, extrasaction="ignore")
        if new:
            self._w.writeheader()

    def write(self, row: dict) -> None:
        self._w.writerow(row)
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def load_done_docids(index_csv: Path) -> Set[str]:
    done: Set[str] = set()
    if index_csv.exists():
        with index_csv.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") in ("downloaded", "already_in_sweep", "not_pdf"):
                    done.add(row["docid"])
    return done


# --------------------------------------------------------------------------- #
# term list
# --------------------------------------------------------------------------- #
def build_terms(extra: Iterable[str], use_dc_targets: bool) -> List[str]:
    terms: List[str] = list(OHIO_TERMS)
    if use_dc_targets:
        for t in dc_targets.entity_terms("OH"):
            t = t.upper().strip()
            if len(t) < 4 or t in SKIP_TERMS or t.lower() in dc_targets.STOPWORDS:
                continue
            terms.append(t)
            words = t.split()
            if len(words) >= 3:
                terms.append(" ".join(words[:2]))
    terms.extend(t.upper().strip() for t in extra if t.strip())
    seen: Set[str] = set()
    unique: List[str] = []
    for t in terms:
        if t not in seen and t.strip("* "):
            seen.add(t)
            unique.append(t)
    return unique


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    ap.add_argument("--sweep-index", type=Path, default=SWEEP_INDEX)
    ap.add_argument("--sweep-dir", type=Path, default=SWEEP_DIR)
    ap.add_argument("--terms", default="", help="Comma-separated extra entity terms (use *x* for substring)")
    ap.add_argument("--only-terms", action="store_true", help="Search ONLY --terms (skip built-in lists)")
    ap.add_argument("--no-dc-targets", action="store_true", help="Skip dc_targets.entity_terms('OH')")
    ap.add_argument("--fulltext-terms", default="data center,datacenter",
                    help="Comma-separated full-text terms ('' to disable)")
    ap.add_argument("--facility-ids", default="", help="Comma-separated facility IDs to add directly")
    ap.add_argument("--program", default="AIR PERMIT", help="Program filter ('' = all programs)")
    ap.add_argument("--max-facilities-per-term", type=int, default=40,
                    help="Terms returning more distinct facilities are treated as generic")
    ap.add_argument("--refresh-discovery", action="store_true", help="Ignore discovery.json cache")
    ap.add_argument("--discover-only", action="store_true", help="Stop after discovery + facility pull")
    ap.add_argument("--no-download", action="store_true", help="Index documents but do not fetch PDFs")
    ap.add_argument("--refetch-trimmed", action="store_true",
                    help="Download docs whose sweep file was trimmed (default: flag only)")
    ap.add_argument("--limit", type=int, default=0, help="Stop after N index rows this run (0 = no limit)")
    ap.add_argument("--max-downloads", type=int, default=2500, help="Cap on PDF downloads this run")
    ap.add_argument("--delay", type=float, default=0.5, help="Seconds between downloads")
    ap.add_argument("--search-delay", type=float, default=1.0, help="Seconds between portal searches")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    out_dir: Path = args.output_dir.expanduser().resolve()
    pdf_dir = out_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    index_csv = out_dir / "oh_epa_dc_permits_index.csv"
    facilities_csv = out_dir / "oh_epa_dc_facilities.csv"
    candidates_csv = out_dir / "oh_epa_dc_candidates.csv"
    discovery_json = out_dir / "discovery.json"
    logger.add(out_dir / "download.log", level="INFO", rotation="20 MB")

    portal = Portal(timeout=args.timeout, delay=args.search_delay)

    # ---------------- discovery ----------------
    extra_terms = [t for t in args.terms.split(",") if t.strip()]
    terms = build_terms(extra_terms, use_dc_targets=not args.no_dc_targets) if not args.only_terms \
        else [t.upper().strip() for t in extra_terms]
    ft_terms = [t.strip() for t in args.fulltext_terms.split(",") if t.strip()]
    logger.info(f"{len(terms)} entity terms, {len(ft_terms)} full-text terms")

    cache: Dict = {"terms": {}}
    if discovery_json.exists() and not args.refresh_discovery:
        cache = json.loads(discovery_json.read_text())
        cache.setdefault("terms", {})

    def run_cached(key: str, **criteria) -> List[Dict[str, str]]:
        if key in cache["terms"]:
            return cache["terms"][key]["rows"]
        try:
            rows = portal.search(max_pages=5, **criteria)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{key}: search failed: {exc}; resetting session")
            portal.session = Portal._new_session()
            time.sleep(5)
            rows = portal.search(max_pages=5, **criteria)
        cache["terms"][key] = {"rows": rows, "n": len(rows), "ts": time.time()}
        discovery_json.write_text(json.dumps(cache))
        time.sleep(args.search_delay)
        return rows

    # facility_id -> info; docid -> row (for rows w/o facility id or from entity searches)
    facilities: Dict[str, Dict] = {}
    candidates: Dict[str, Dict] = {}
    direct_rows: Dict[str, Dict[str, str]] = {}

    def add_hit(fid: str, row: Dict[str, str], source: str, store: Dict[str, Dict]) -> None:
        info = store.setdefault(fid, {"entity_names": set(), "county": row.get("county", ""),
                                      "sources": set(), "n": 0})
        info["entity_names"].add(row.get("entity_name", ""))
        info["sources"].add(source)
        info["n"] += 1

    for i, term in enumerate(terms, 1):
        key = f"entity:{term}"
        rows = run_cached(key, entity=term, program=args.program)
        fids = {r["facility_id"] for r in rows}
        generic = len(fids) > args.max_facilities_per_term
        kept = 0
        for r in rows:
            fid = r["facility_id"]
            if generic and not DC_NAME_PATTERN.search(r["entity_name"]):
                if fid:
                    add_hit(fid, r, key, candidates)
                continue
            kept += 1
            if fid:
                add_hit(fid, r, key, facilities)
            else:
                direct_rows.setdefault(r["docid"], dict(r, match_sources=key))
        flag = " [GENERIC -> DC-name filter]" if generic else ""
        if rows:
            logger.info(f"[{i}/{len(terms)}] {term!r}: {len(rows)} rows, {len(fids)} facilities, kept {kept}{flag}")

    for term in ft_terms:
        key = f"fulltext:{term}"
        rows = run_cached(key, fulltext=term, program=args.program)
        fids = {r["facility_id"] for r in rows}
        kept = 0
        for r in rows:
            fid = r["facility_id"]
            if fid and (fid in facilities or DC_NAME_PATTERN.search(r["entity_name"])):
                add_hit(fid, r, key, facilities)
                kept += 1
            elif fid:
                add_hit(fid, r, key, candidates)
        logger.info(f"full-text {term!r}: {len(rows)} rows, {len(fids)} facilities, kept {kept}")

    for fid in [f.strip() for f in args.facility_ids.split(",") if f.strip()]:
        facilities.setdefault(fid, {"entity_names": set(), "county": "", "sources": {"arg:--facility-ids"}, "n": 0})

    candidates = {k: v for k, v in candidates.items() if k not in facilities}
    logger.success(f"Discovery: {len(facilities)} matched facilities, {len(direct_rows)} facility-less rows, "
                   f"{len(candidates)} candidate facilities (not downloaded)")

    with candidates_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CANDIDATE_FIELDS)
        w.writeheader()
        for fid, info in sorted(candidates.items(), key=lambda kv: -kv[1]["n"]):
            w.writerow({"facility_id": fid, "entity_names": " | ".join(sorted(info["entity_names"])),
                        "county": info["county"], "n_hits": info["n"],
                        "match_sources": " | ".join(sorted(info["sources"]))})

    # ---------------- per-facility pull ----------------
    docs: Dict[str, Dict[str, str]] = dict(direct_rows)
    fac_rows: List[Dict] = []
    for i, (fid, info) in enumerate(sorted(facilities.items()), 1):
        key = f"facility:{fid}"
        rows = run_cached(key, secondary=fid, program=args.program)
        for r in rows:
            info["entity_names"].add(r["entity_name"])
            if not info["county"]:
                info["county"] = r.get("county", "")
            docs.setdefault(r["docid"], dict(r, match_sources=" | ".join(sorted(info["sources"]))))
        names = " | ".join(sorted(n for n in info["entity_names"] if n))
        fac_rows.append({"facility_id": fid, "entity_names": names, "county": info["county"],
                         "n_docs": len(rows), "match_sources": " | ".join(sorted(info["sources"])),
                         "is_dc_name": int(bool(DC_NAME_PATTERN.search(names)))})
        logger.info(f"[{i}/{len(facilities)}] facility {fid} ({names[:60]}): {len(rows)} docs")

    with facilities_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FACILITY_FIELDS)
        w.writeheader()
        for r in sorted(fac_rows, key=lambda x: (-x["n_docs"], x["facility_id"])):
            w.writerow(r)
    logger.success(f"{len(docs)} unique documents across {len(facilities)} facilities "
                   f"-> {facilities_csv.name}")
    if args.discover_only:
        return 0

    # ---------------- download ----------------
    sweep = load_sweep(args.sweep_index, args.sweep_dir)
    done = load_done_docids(index_csv)
    logger.info(f"Resume: {len(done)} docids already in {index_csv.name}")
    writer = IndexWriter(index_csv, INDEX_FIELDS)

    def sort_key(r: Dict[str, str]):
        return (r.get("entity_name", ""), iso_date(r.get("doc_date", "")) or "0000", r["docid"])

    todo = [r for r in sorted(docs.values(), key=sort_key) if r["docid"] not in done]
    logger.info(f"{len(todo)} documents to process ({len(docs) - len(todo)} already indexed)")

    n_rows = n_dl = n_sweep = n_fail = 0
    try:
        for r in todo:
            if args.limit and n_rows >= args.limit:
                logger.info(f"--limit {args.limit} reached")
                break
            docid = r["docid"]
            row = {k: r.get(k, "") for k in INDEX_FIELDS}
            row["already_in_sweep"] = 0
            row["sweep_file_present"] = 0
            in_sweep = docid in sweep
            sweep_file = sweep.get(docid)
            if in_sweep and (sweep_file is not None or not args.refetch_trimmed):
                row.update(status="already_in_sweep", already_in_sweep=1,
                           sweep_file_present=int(sweep_file is not None),
                           local_path=str(sweep_file) if sweep_file is not None else "")
                n_sweep += 1
            elif args.no_download:
                row["status"] = "indexed"
            elif n_dl >= args.max_downloads:
                logger.info(f"--max-downloads {args.max_downloads} reached")
                break
            else:
                row["already_in_sweep"] = int(in_sweep)
                stem = clean_filename(f"{docid}_{r['title']}")[:200]
                dest = pdf_dir / f"{stem}.pdf"
                if dest.exists():
                    ok = True
                else:
                    ok = portal.download(docid, dest)
                    time.sleep(args.delay)
                if ok:
                    row.update(status="downloaded", local_path=str(dest))
                    n_dl += 1
                    logger.info(f"downloaded {dest.name[:110]}")
                else:
                    row["status"] = "failed_download"
                    n_fail += 1
            writer.write(row)
            n_rows += 1
    finally:
        writer.close()

    logger.success("=" * 60)
    logger.success("OHIO EPA DATA-CENTER PERMIT PULL COMPLETE")
    logger.success(f"facilities matched: {len(facilities)}   documents known: {len(docs)}")
    logger.success(f"this run: rows {n_rows}, downloaded {n_dl}, already_in_sweep {n_sweep}, failed {n_fail}")
    logger.success(f"index: {index_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
