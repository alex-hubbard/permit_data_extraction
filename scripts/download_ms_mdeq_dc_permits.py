#!/usr/bin/env python3
"""Download Mississippi MDEQ air permit documents for data centers.

MDEQ publishes permit records through enSearch Online
(https://opcgis.deq.state.ms.us/ensearchonline/, classic ASP.NET WebForms:
each search is a form POST that must echo the page's __VIEWSTATE /
__EVENTVALIDATION tokens). The pages this script drives:

  epd-active-permits-and-coverages.aspx
        POST uxName (substring match), uxSIC (4 digits), uxCounty$uxCounty
        (3-digit FIPS), uxPermitType ("Air - SMOP", "Air - Title V", ...),
        uxSubmit=SEARCH.  Returns ONE #uxGrid table with every row (no
        paging): AI ID | Agency Interest Name | Permit # | Permit Type | City |
        County, each linked to ai_info.aspx?ai=<AI ID>.
  ai_info.aspx?ai=<id>
        Facility page: SIC / county / branch (#uxGeneral), addresses
        (#uxAddress), alternate ids+names (#uxAlt), active individual permits
        with the final permit document (#uxACTIVEPER), general-permit
        coverages with their document (#uxACTIVEGNP), permit activity status
        lines (#uxPER) and general-permit NOI PDFs (#uxGNP).
  epd-permits-at-public-notice.aspx
        GET.  Permits currently at public notice with the public notice
        (dt=pnotice), draft permit (dt=dpermit) and permit rationale / EPA
        analysis (dt=eparat) documents.  Rolling window only.
  recently-issued-permits-certifications.aspx
        GET.  Permits issued in the last ~90 days with final permit documents.
  epd-activity-search.aspx
        POST uxActionType (2020 receive individual application, 2160 issue
        individual permit, 2021 / 2750 general-permit NOIs and coverages),
        uxStartDate, uxEndDate (m/d/yyyy).  One sentence per action linked to
        ai_info.aspx -- no documents, but it reveals pending applications.
  get_doc.aspx?dt=<prefix>&id=<doc id>
        The file itself (PDF, or RTF for many notices / rationales).  `dt` only
        sets the download filename prefix; the id alone selects the document.

agency-interest-search.aspx (the "facility search") is broken server-side
(HTTP 500 from MDEQ's own event-log writer) and is not used.  There is no
NAICS search; SIC 7374 (computer processing / data preparation) is the
data-center code MDEQ assigns.

Strategy
--------
1. Discover: enumerate every active air permit statewide (5 permit types,
   ~560 rows), then sweep SIC codes (7374 ...), operator-name searches
   (dc_targets.entity_terms("MS") plus Mississippi aliases such as MZX Tech,
   the xAI Southaven entity), the activity feed since --since, and the
   public-notice / recently-issued reports.
2. Flag data-center facilities: SIC 737x, a data-center name pattern, or an
   operator term in the name.  Weak terms ("Amazon" alone, which also hits
   fulfillment centers) only count together with a data-center pattern / SIC.
3. Fetch ai_info.aspx for every flagged AI ID and collect all document links.
4. Download air-program documents (every program with --all-programs),
   sniffing magic bytes for .pdf / .rtf / .doc.  The index CSV is appended
   row by row as files land, so a rerun resumes where it left off.

Output layout
-------------
  <RAW_DATA_DIR>/ms_mdeq_dc_permits/
    pdfs/<ai_id>_<permit_no>_<doc_kind>_<doc_id>.pdf|.rtf
    ms_mdeq_dc_permits_index.csv        one row per document (appended as downloaded)
    ms_mdeq_dc_facilities.csv           flagged facilities, match reason, status lines
    ms_mdeq_air_permits_full_index.csv  every active air permit row statewide
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR

import dc_targets

BASE = "https://opcgis.deq.state.ms.us/ensearchonline/"
OUTPUT_DIR = RAW_DATA_DIR / "ms_mdeq_dc_permits"

AIR_PERMIT_TYPES = [
    "Air - Construction PSD",
    "Air - Construction Permit",
    "Air - SMOP",
    "Air - State Operating Permit",
    "Air - Title V",
]
DEFAULT_SICS = ["7374"]
DC_SICS = {"7370", "7371", "7372", "7373", "7374", "7375", "7376", "7377", "7378", "7379"}

# Mississippi-specific entities / aliases not derivable from dc_targets.
#   MZX Tech LLC  = xAI's entity at the former Duke Energy / KGen / BTEC
#                   Southaven Energy Facility, 2875 Stanton Rd, Southaven
#                   (Air Construction PSD 0680-00119, issued 03/11/2026).
#   ADP Rankin Data Hub = AVAIO Digital Partners, Brandon (Rankin County).
MS_EXTRA_TERMS = [
    "MZX Tech", "ADP Rankin", "Data Hub", "Data Cent", "Datacenter",
    "MacroHard", "Colossus", "Amazon Data", "Amazon Web", "xAI Corp", "X AI",
    "BTEC Southaven", "Southaven Energy",
]
# Terms that also match unrelated facilities; only accepted with a DC pattern
# or SIC (Amazon fulfillment centers, "Switch"gear, etc.).
WEAK_TERMS = {
    "amazon", "amazon com inc", "amazon com", "facebook", "oracle", "switch",
    "ntt", "qts", "iren", "hut 8", "xai", "apple inc", "google", "microsoft",
    "design llc",  # Google's Oregon alias; hits "Gulf Coast Development and Design LLC"
}
# Too short / generic for the portal's substring search.
SKIP_SEARCH_TERMS = {"switch", "ntt", "qts", "iren", "hut 8", "xai", "x ai", "design llc"}

DC_NAME_PATTERN = re.compile(
    r"data ?cent|data ?hub|datacent|colocation|co-location|hyperscale|server farm",
    re.IGNORECASE,
)

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0 Safari/537.36"}

INDEX_FIELDS = [
    "ai_id", "facility_name", "sic", "county", "city", "address",
    "permit_number", "permit_type", "program", "doc_kind", "doc_date",
    "doc_id", "doc_url", "discovered_via", "file_path", "content_type",
    "bytes", "downloaded_at",
]
FACILITY_FIELDS = [
    "ai_id", "facility_name", "sic", "county", "city", "address",
    "mailing_address", "branch", "match_reason", "discovered_via",
    "alt_names", "permits", "status_lines", "n_docs",
]
FULL_INDEX_FIELDS = ["permit_type_searched", "ai_id", "facility_name",
                     "permit_number", "permit_type", "city", "county", "is_dc"]

AI_LINK_RE = re.compile(r"ai_info\.aspx\?ai=(\d+)", re.IGNORECASE)
DOC_LINK_RE = re.compile(r"get_doc\.aspx\?dt=(\w+)&(?:amp;)?id=(\d+)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Portal client
# --------------------------------------------------------------------------- #
class Portal:
    def __init__(self, delay: float, timeout: int):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay
        self.timeout = timeout
        self.n_requests = 0

    def _sleep(self) -> None:
        self.n_requests += 1
        time.sleep(self.delay)

    def get(self, page: str, stream: bool = False) -> requests.Response:
        r = self.s.get(urljoin(BASE, page), timeout=self.timeout, stream=stream)
        r.raise_for_status()
        self._sleep()
        return r

    def post_form(self, page: str, fields: Dict[str, str]) -> str:
        """GET the page for its WebForms tokens, then POST the search."""
        g = self.get(page)
        soup = BeautifulSoup(g.text, "html.parser")
        form = soup.find("form", id=lambda x: x and x not in ("searchform", "searchformSide"))
        if form is None:
            raise RuntimeError(f"{page}: search form not found")
        data: Dict[str, str] = {}
        for inp in form.find_all("input"):
            name, typ = inp.get("name"), (inp.get("type") or "text").lower()
            if not name or typ in ("submit", "button", "reset", "image"):
                continue
            data[name] = inp.get("value", "")
        for sel in form.find_all("select"):
            opt = sel.find("option", selected=True) or sel.find("option")
            data[sel["name"]] = opt.get("value", "") if opt else ""
        data.update(fields)
        data["uxSubmit"] = "SEARCH"
        r = self.s.post(urljoin(BASE, page), data=data, timeout=self.timeout,
                        headers={"Referer": urljoin(BASE, page)})
        r.raise_for_status()
        self._sleep()
        return r.text


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def _ai_from_href(href: str) -> str:
    m = AI_LINK_RE.search(href or "")
    return m.group(1) if m else ""


def parse_permit_grid(html: str) -> List[Dict[str, str]]:
    """Rows of #uxGrid on epd-active-permits-and-coverages.aspx."""
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.find(id="uxGrid")
    rows: List[Dict[str, str]] = []
    if grid is None:
        return rows
    for tr in grid.find_all("tr"):
        tds = tr.find_all("td")
        link = tr.find("a", href=AI_LINK_RE)
        if len(tds) < 6 or link is None:
            continue
        vals = [td.get_text(" ", strip=True) for td in tds]
        rows.append({
            "ai_id": _ai_from_href(link["href"]) or vals[0],
            "facility_name": vals[1], "permit_number": vals[2],
            "permit_type": vals[3], "city": vals[4], "county": vals[5],
        })
    return rows


def result_count(html: str) -> Optional[int]:
    hdr = BeautifulSoup(html, "html.parser").find(id="uxTableResultHeader")
    m = re.search(r"Results:\s*(\d+)", hdr.get_text(" ", strip=True)) if hdr else None
    return int(m.group(1)) if m else None


def _table_rows(soup: BeautifulSoup, table_id: str) -> List[List]:
    t = soup.find(id=table_id)
    if t is None:
        return []
    out = []
    for tr in t.find_all("tr"):
        tds = tr.find_all("td")
        if not tds or tds[0].get("class") == ["resultheader"]:
            continue
        out.append(tds)
    return out


def parse_ai_info(html: str, ai_id: str) -> Dict:
    """Facility metadata + document links from ai_info.aspx."""
    soup = BeautifulSoup(html, "html.parser")
    info: Dict = {"ai_id": ai_id, "sic": "", "county": "", "branch": "",
                  "address": "", "mailing_address": "", "alt_names": [],
                  "permits": [], "status_lines": [], "docs": []}
    for tds in _table_rows(soup, "uxGeneral"):
        vals = [td.get_text(" ", strip=True) for td in tds]
        if len(vals) >= 4:
            info["branch"], info["sic"], info["county"] = vals[1], vals[2], vals[3]
    for tds in _table_rows(soup, "uxAddress"):
        vals = [td.get_text(" ", strip=True) for td in tds]
        if len(vals) >= 2:
            info["address"], info["mailing_address"] = vals[0], vals[1]
        elif vals:
            info["address"] = vals[0]
    for tds in _table_rows(soup, "uxAlt"):
        vals = [td.get_text(" ", strip=True) for td in tds]
        if len(vals) >= 3:
            info["alt_names"].append(f"{vals[1]} [{vals[2]} {vals[0]}]")
    # Active permits (individual + general) with their final permit document.
    for table_id, program_hint in (("uxACTIVEPER", ""), ("uxACTIVEGNP", "General Permit")):
        for tds in _table_rows(soup, table_id):
            vals = [td.get_text(" ", strip=True) for td in tds]
            if len(vals) < 3:
                continue
            ptype, pnum, pdate = vals[0], vals[1], vals[2]
            program = program_hint or ptype.split(" - ")[0].strip()
            info["permits"].append(f"{ptype} {pnum} ({pdate})")
            for a in tds[-1].find_all("a", href=DOC_LINK_RE):
                m = DOC_LINK_RE.search(a["href"])
                info["docs"].append({
                    "permit_number": pnum, "permit_type": ptype, "program": program,
                    "doc_kind": "final_permit" if table_id == "uxACTIVEPER" else "gp_coverage",
                    "doc_date": pdate, "doc_id": m.group(2), "doc_dt": m.group(1),
                })
    # Permit activity status lines (no documents) + general-permit NOI PDFs.
    for tds in _table_rows(soup, "uxPER"):
        vals = [td.get_text(" ", strip=True) for td in tds]
        if len(vals) >= 5:
            info["status_lines"].append(f"{vals[0]} | {vals[1]} | {vals[2]} | {vals[3]} | {vals[4]}")
    for tds in _table_rows(soup, "uxGNP"):
        vals = [td.get_text(" ", strip=True) for td in tds]
        if len(vals) >= 5:
            info["status_lines"].append(f"{vals[0]} | {vals[1]} | {vals[2]} | {vals[3]} | {vals[4]}")
            for a in tds[-1].find_all("a", href=DOC_LINK_RE):
                m = DOC_LINK_RE.search(a["href"])
                info["docs"].append({
                    "permit_number": vals[2], "permit_type": vals[1], "program": vals[0],
                    "doc_kind": "gp_noi", "doc_date": vals[3], "doc_id": m.group(2),
                    "doc_dt": m.group(1),
                })
    return info


def parse_public_notice(html: str) -> List[Dict]:
    """Rows of epd-permits-at-public-notice.aspx with their three documents."""
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.find(id="uxGrid")
    out = []
    if grid is None:
        return out
    for tr in grid.find_all("tr"):
        link = tr.find("a", href=AI_LINK_RE)
        if link is None:
            continue
        vals = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        # Name | Permit Type | City | County | End Date | Notice | Draft | Other
        row = {"ai_id": _ai_from_href(link["href"]),
               "facility_name": vals[0] if vals else link.get_text(strip=True),
               "permit_type": vals[1] if len(vals) > 1 else "",
               "city": vals[2] if len(vals) > 2 else "",
               "county": vals[3] if len(vals) > 3 else "",
               "end_date": vals[4] if len(vals) > 4 else "", "docs": []}
        for a in tr.find_all("a", href=DOC_LINK_RE):
            m = DOC_LINK_RE.search(a["href"])
            kind = {"pnotice": "public_notice", "dpermit": "draft_permit",
                    "eparat": "rationale"}.get(m.group(1).lower(), m.group(1).lower())
            row["docs"].append({"doc_id": m.group(2), "doc_dt": m.group(1), "doc_kind": kind})
        out.append(row)
    return out


def parse_recently_issued(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.find(id="uxGrid")
    out = []
    if grid is None:
        return out
    for tr in grid.find_all("tr"):
        link = tr.find("a", href=AI_LINK_RE)
        doc = tr.find("a", href=DOC_LINK_RE)
        if link is None:
            continue
        vals = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        # Permit(doc) | Agency Interest Name | Permit/Certification Type | City | County
        row = {"ai_id": _ai_from_href(link["href"]),
               "facility_name": vals[1] if len(vals) > 1 else "",
               "permit_type": vals[2] if len(vals) > 2 else "",
               "city": vals[3] if len(vals) > 3 else "",
               "county": vals[4] if len(vals) > 4 else "", "docs": []}
        if doc is not None:
            m = DOC_LINK_RE.search(doc["href"])
            row["docs"].append({"doc_id": m.group(2), "doc_dt": m.group(1), "doc_kind": "final_permit"})
        out.append(row)
    return out


ACTIVITY_RE = re.compile(r"^(?P<name>.+?)\s,\s(?P<city>[^,]+?),\s*MS\s+(?P<action>.+)$")


def parse_activity(html: str) -> List[Dict]:
    """Sentences of epd-activity-search.aspx (#uxReport), one per action."""
    soup = BeautifulSoup(html, "html.parser")
    rep = soup.find(id="uxReport") or soup.find(id="uxGrid")
    out = []
    if rep is None:
        return out
    for tr in rep.find_all("tr"):
        link = tr.find("a", href=AI_LINK_RE)
        if link is None:
            continue
        text = tr.get_text(" ", strip=True)
        m = ACTIVITY_RE.match(text)
        # Rows are grouped by county: the first row of each group is prefixed
        # with the county name, so the parsed name is only approximate.
        out.append({"ai_id": _ai_from_href(link["href"]),
                    "facility_name": (m.group("name") if m else text).strip(),
                    "city": m.group("city").strip() if m else "",
                    "action": m.group("action").strip() if m else "",
                    "sentence": text})
    return out


# --------------------------------------------------------------------------- #
# Data-center matching
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 &-]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def build_terms(extra: Iterable[str] = ()) -> List[str]:
    terms = dc_targets.entity_terms("MS") + list(MS_EXTRA_TERMS) + list(extra)
    seen, out = set(), []
    for t in terms:
        key = _norm(t)
        if len(key) >= 4 and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def match_reason(name: str, sic: str, terms: List[str]) -> str:
    """Why a facility counts as a data center ('' if it does not)."""
    text = _norm(name)
    reasons = []
    if sic and sic in DC_SICS:
        reasons.append(f"sic:{sic}")
    if DC_NAME_PATTERN.search(name or ""):
        reasons.append("name_pattern")
    strong, weak = [], []
    for t in terms:
        key = _norm(t)
        if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", text):
            (weak if key in WEAK_TERMS else strong).append(t)
    if strong:
        reasons.append("term:" + "|".join(strong[:3]))
    elif weak and reasons:
        reasons.append("weak_term:" + "|".join(weak[:3]))
    return ";".join(reasons)


# --------------------------------------------------------------------------- #
# Downloads / index
# --------------------------------------------------------------------------- #
def sniff_ext(content: bytes, content_type: str) -> str:
    if content.startswith(b"%PDF"):
        return ".pdf"
    if content.startswith(b"{\\rtf"):
        return ".rtf"
    if content[:2] == b"\xd0\xcf" or "msword" in (content_type or ""):
        return ".doc"
    if content.startswith(b"PK"):
        return ".docx"
    return ""


def load_index(index_path: Path) -> Dict[str, Dict[str, str]]:
    done: Dict[str, Dict[str, str]] = {}
    if not index_path.exists():
        return done
    with open(index_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            fp = row.get("file_path", "")
            if row.get("doc_id") and fp and Path(fp).exists():
                done[row["doc_id"]] = row
    return done


def append_index(index_path: Path, row: Dict[str, str]) -> None:
    new = not index_path.exists() or index_path.stat().st_size == 0
    with open(index_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)
        f.flush()


def write_csv(path: Path, fields: List[str], rows: Iterable[Dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def is_air_doc(doc: Dict[str, str]) -> bool:
    blob = f"{doc.get('program', '')} {doc.get('permit_type', '')}".lower()
    return blob.startswith("air") or " air " in f" {blob} " or blob.startswith("air-")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--terms", default="", help="Comma-separated operator terms (default: dc_targets MS + MS aliases)")
    ap.add_argument("--extra-terms", default="", help="Comma-separated terms to add to the defaults")
    ap.add_argument("--sics", default=",".join(DEFAULT_SICS), help="Comma-separated SIC codes to sweep")
    ap.add_argument("--ai-ids", default="", help="Comma-separated AI IDs to force-include")
    ap.add_argument("--since", default="2023-01-01", help="Activity-feed start date (YYYY-MM-DD)")
    ap.add_argument("--skip-air-sweep", action="store_true", help="Skip the statewide air permit enumeration")
    ap.add_argument("--skip-activity", action="store_true", help="Skip the activity feed")
    ap.add_argument("--all-programs", action="store_true",
                    help="Download water / stormwater documents of matched facilities too (default: air only)")
    ap.add_argument("--limit", type=int, default=0, help="Stop after downloading this many new documents (0 = no limit)")
    ap.add_argument("--max-docs", type=int, default=1000, help="Hard cap on documents downloaded per run")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    index_path = OUTPUT_DIR / "ms_mdeq_dc_permits_index.csv"
    logger.add(OUTPUT_DIR / "download.log", rotation="5 MB", level="INFO")

    if args.terms:
        terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    else:
        terms = build_terms([t.strip() for t in args.extra_terms.split(",") if t.strip()])
    search_terms = [t for t in terms if _norm(t) not in SKIP_SEARCH_TERMS]
    logger.info(f"{len(terms)} match terms, {len(search_terms)} portal search terms")

    portal = Portal(args.delay, args.timeout)

    # ---- 1. discovery --------------------------------------------------- #
    candidates: Dict[str, Dict] = {}   # ai_id -> facility row (first seen)
    full_index: List[Dict] = []

    def note(row: Dict, via: str) -> None:
        ai = row.get("ai_id")
        if not ai:
            return
        c = candidates.setdefault(ai, {
            "ai_id": ai, "facility_name": row.get("facility_name", ""),
            "city": row.get("city", ""), "county": row.get("county", ""),
            "discovered_via": set(), "grid_permits": set(), "sic": "",
            "status_lines": [], "extra_docs": [],
        })
        if len(row.get("facility_name", "")) > len(c["facility_name"]):
            c["facility_name"] = row["facility_name"]
        c["discovered_via"].add(via)
        if row.get("permit_number"):
            c["grid_permits"].add(f"{row.get('permit_type', '')} {row['permit_number']}")
        for d in row.get("docs", []):
            d = dict(d)
            d.setdefault("permit_type", row.get("permit_type", ""))
            d.setdefault("program", row.get("permit_type", "").split(" - ")[0].split(" ")[0])
            d.setdefault("permit_number", "")
            d.setdefault("doc_date", row.get("end_date", ""))
            c["extra_docs"].append(d)
        if row.get("sentence"):
            c["status_lines"].append(row["sentence"])

    if not args.skip_air_sweep:
        for ptype in AIR_PERMIT_TYPES:
            try:
                html = portal.post_form("epd-active-permits-and-coverages.aspx", {"uxPermitType": ptype})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"air sweep {ptype!r} failed: {e}")
                continue
            rows = parse_permit_grid(html)
            logger.info(f"air sweep {ptype!r}: {len(rows)} rows (portal says {result_count(html)})")
            for r in rows:
                r["permit_type_searched"] = ptype
                full_index.append(r)
                if match_reason(r["facility_name"], "", terms):
                    note(r, f"air_sweep:{ptype}")

    for sic in [s.strip() for s in args.sics.split(",") if s.strip()]:
        try:
            html = portal.post_form("epd-active-permits-and-coverages.aspx", {"uxSIC": sic})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SIC {sic} failed: {e}")
            continue
        rows = parse_permit_grid(html)
        logger.info(f"SIC {sic}: {len(rows)} rows")
        for r in rows:
            r["sic"] = sic
            note(r, f"sic:{sic}")
            candidates[r["ai_id"]]["sic"] = sic

    for i, term in enumerate(search_terms, 1):
        try:
            html = portal.post_form("epd-active-permits-and-coverages.aspx", {"uxName": term})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{i}/{len(search_terms)}] {term!r} failed: {e}")
            continue
        rows = parse_permit_grid(html)
        hits = [r for r in rows if match_reason(r["facility_name"], "", terms)]
        if rows:
            logger.info(f"[{i}/{len(search_terms)}] {term!r}: {len(rows)} rows, {len(hits)} data-center matches")
        for r in hits:
            note(r, f"name:{term}")

    if not args.skip_activity:
        start = dt.datetime.strptime(args.since, "%Y-%m-%d")
        end = dt.date.today()
        fmt = lambda d: f"{d.month}/{d.day}/{d.year}"  # noqa: E731
        for code, label in (("2020", "receive_individual_application"), ("2160", "issue_individual_permit"),
                            ("2021", "receive_gp_noi"), ("2750", "issue_gp_coverage")):
            try:
                html = portal.post_form("epd-activity-search.aspx", {
                    "uxActionType": code, "uxStartDate": fmt(start), "uxEndDate": fmt(end)})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"activity {label} failed: {e}")
                continue
            rows = parse_activity(html)
            hits = [r for r in rows if match_reason(r["facility_name"], "", terms)]
            logger.info(f"activity {label} since {args.since}: {len(rows)} actions, {len(hits)} data-center matches")
            for r in hits:
                note(r, f"activity:{label}")

    try:
        pn_rows = parse_public_notice(portal.get("epd-permits-at-public-notice.aspx").text)
        logger.info(f"public notice page: {len(pn_rows)} permits at notice")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"public notice page failed: {e}")
        pn_rows = []
    try:
        ri_rows = parse_recently_issued(portal.get("recently-issued-permits-certifications.aspx").text)
        logger.info(f"recently issued page: {len(ri_rows)} permits")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"recently issued page failed: {e}")
        ri_rows = []

    forced = {a.strip() for a in args.ai_ids.split(",") if a.strip()}
    for a in forced:
        note({"ai_id": a, "facility_name": ""}, "forced")

    # Public-notice / recently-issued rows count if the facility is already a
    # candidate or the name itself matches.
    for via, rows in (("public_notice", pn_rows), ("recently_issued", ri_rows)):
        for r in rows:
            if r["ai_id"] in candidates or match_reason(r["facility_name"], "", terms):
                note(r, via)

    logger.success(f"{len(candidates)} candidate facilities; {len(full_index)} statewide air permit rows")

    # ---- 2. facility pages ------------------------------------------------ #
    facilities: List[Dict] = []
    docs: Dict[str, Dict] = {}
    for i, (ai, c) in enumerate(sorted(candidates.items(), key=lambda kv: int(kv[0])), 1):
        try:
            info = parse_ai_info(portal.get(f"ai_info.aspx?ai={ai}").text, ai)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{i}/{len(candidates)}] ai_info {ai} failed: {e}")
            info = {"ai_id": ai, "sic": c["sic"], "county": c["county"], "branch": "", "address": "",
                    "mailing_address": "", "alt_names": [], "permits": [], "status_lines": [], "docs": []}
        name = c["facility_name"] or (info["alt_names"][0].split(" [")[0] if info["alt_names"] else "")
        sic = info["sic"] or c["sic"]
        reason = match_reason(name, sic, terms) or ("forced" if ai in forced else "")
        if not reason and info["alt_names"]:
            reason = match_reason(" ; ".join(info["alt_names"]), sic, terms)
        if not reason:
            logger.info(f"[{i}/{len(candidates)}] AI {ai} {name!r} (SIC {sic}) dropped: no data-center signal")
            continue
        fac = {
            "ai_id": ai, "facility_name": name, "sic": sic,
            "county": info["county"] or c["county"], "city": c["city"],
            "address": info["address"], "mailing_address": info["mailing_address"],
            "branch": info["branch"], "match_reason": reason,
            "discovered_via": "|".join(sorted(c["discovered_via"])),
            "alt_names": " || ".join(info["alt_names"]),
            "permits": " || ".join(info["permits"] or sorted(c["grid_permits"])),
            "status_lines": " || ".join(info["status_lines"] + c["status_lines"]),
        }
        all_docs = info["docs"] + c["extra_docs"]
        for d in all_docs:
            if d["doc_id"] in docs:
                continue
            docs[d["doc_id"]] = {
                "ai_id": ai, "facility_name": name, "sic": sic, "county": fac["county"],
                "city": fac["city"], "address": fac["address"],
                "permit_number": d.get("permit_number", ""), "permit_type": d.get("permit_type", ""),
                "program": d.get("program", ""), "doc_kind": d.get("doc_kind", ""),
                "doc_date": d.get("doc_date", ""), "doc_id": d["doc_id"],
                "doc_url": f"{BASE}get_doc.aspx?dt={d.get('doc_dt', 'pdf')}&id={d['doc_id']}",
                "discovered_via": fac["discovered_via"],
            }
        fac["n_docs"] = sum(1 for d in docs.values() if d["ai_id"] == ai)
        facilities.append(fac)
        logger.info(f"[{i}/{len(candidates)}] AI {ai} {name!r} SIC {sic} {fac['county']}: "
                    f"{fac['n_docs']} docs [{reason}]")

    write_csv(OUTPUT_DIR / "ms_mdeq_dc_facilities.csv", FACILITY_FIELDS, facilities)
    dc_ais = {f["ai_id"] for f in facilities}
    for r in full_index:
        r["is_dc"] = "Y" if r["ai_id"] in dc_ais else ""
    if full_index:
        write_csv(OUTPUT_DIR / "ms_mdeq_air_permits_full_index.csv", FULL_INDEX_FIELDS, full_index)
    logger.success(f"{len(facilities)} data-center facilities, {len(docs)} documents indexed")

    # ---- 3. downloads ----------------------------------------------------- #
    if args.no_download:
        return 0
    done = load_index(index_path)
    to_get = [d for d in docs.values() if args.all_programs or is_air_doc(d)]
    pending = [d for d in to_get if d["doc_id"] not in done]
    logger.info(f"{len(to_get)} documents selected ({'all programs' if args.all_programs else 'air only'}), "
                f"{len(done)} already downloaded, {len(pending)} to fetch")
    cap = args.max_docs if not args.limit else min(args.limit, args.max_docs)
    n_ok = n_fail = 0
    for i, d in enumerate(pending, 1):
        if n_ok >= cap:
            logger.warning(f"document cap {cap} reached; rerun to continue")
            break
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_",
                      f"{d['ai_id']}_{d['permit_number'] or 'NA'}_{d['doc_kind']}_{d['doc_id']}").strip("_")
        try:
            r = portal.get(d["doc_url"])
            ext = sniff_ext(r.content, r.headers.get("Content-Type", ""))
            if not ext:
                logger.warning(f"[{i}/{len(pending)}] doc {d['doc_id']}: unrecognised content "
                               f"({r.headers.get('Content-Type')}, {len(r.content)} bytes) - skipped")
                n_fail += 1
                continue
            dest = pdf_dir / f"{stem}{ext}"
            dest.write_bytes(r.content)
            row = dict(d)
            row.update({"file_path": str(dest), "content_type": r.headers.get("Content-Type", ""),
                        "bytes": str(len(r.content)),
                        "downloaded_at": dt.datetime.now().isoformat(timespec="seconds")})
            append_index(index_path, row)
            n_ok += 1
            logger.info(f"[{i}/{len(pending)}] {dest.name} ({len(r.content) // 1024} KB) "
                        f"{d['facility_name'][:50]!r} {d['permit_type']}")
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            logger.warning(f"[{i}/{len(pending)}] doc {d['doc_id']} failed: {e}")
    logger.success(f"Downloaded {n_ok} new documents ({n_fail} failed); index at {index_path}; "
                   f"{portal.n_requests} HTTP requests this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
