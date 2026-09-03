#!/usr/bin/env python3
"""Download Washington State air-permit documents for data centers.

Washington air permitting is split between the Department of Ecology (counties
with no local agency: Grant/Douglas/Chelan/Kittitas/... -> Quincy, East
Wenatchee, Moses Lake, Malaga data centers) and local clean air agencies
(Puget Sound Clean Air Agency for King/Pierce/Snohomish/Kitsap, Southwest
Clean Air Agency for Clark/Cowlitz/Lewis/Skamania/Wahkiakum, Spokane Regional
Clean Air Agency for Spokane County).  This script sweeps every lane that has
a public document store and records the agency on each index row.

Lanes and request shapes (worked out with curl, Sept 2026)
----------------------------------------------------------
ecology_page  Ecology's "Data centers" page lists every data center it
              permits, by city, with approval orders / TSDs / NOC
              applications / health impact assessments:
                GET https://ecology.wa.gov/air-climate/air-quality/data-centers
              Accordion: button.accordion-title = "City - Facility", then
              div.accordion-content with h4/strong permit-number groups and
              <li> items.  Links are Kentico media paths
              (/Asset-Collections/Doc-Assets/...) or short redirect URLs
              (/20260406sabeyinternoctsd) that 301 to
              /getattachment/<guid>/<file>.pdf; a few point at
              apps.ecology.wa.gov/publications/SummaryPages/<id>.html whose
              PDF is apps.ecology.wa.gov/publications/documents/<id>.pdf.
              Older approval orders are listed without a link (not posted).
ecap          Ecology Air Permit Search (Angular SPA, JSON API; Title V AOP
              and PSD documents only -- 63 facilities, no data centers today):
                POST /ecap/api/documents/search-facilities/<page0>/<size>
                     {"criteria":{"DocumentTypeId":0,"IssuanceDate":{...},
                      "ShowCurrentPermits":false,"DocumentMetaData":{}}}
                GET  /ecap/api/documents/facility-documents/<fid>/0/0/<n>/true
                GET  /ecap/api/documents/document-data/<DocumentId>  -> PDF
pscaa         No permit database.  CivicPlus site search
                GET https://pscleanair.gov/Search/Results?searchPhrase=<term>
              returns DocumentCenter/View/<id> hits (draft/final orders of
              approval, worksheets, applications) for projects PSCAA has put
              out for comment; plus the "Permits Open for Comment" (/175) and
              "Recent Permitting Projects" (/460) pages whose per-project
              subpages carry the order/application PDFs.
swcaa         Classic ASP e-Permits:
                GET  https://www.swcleanair.gov/permits/permitADPsearch.asp
                     (select[name=PlantID] lists every registered plant)
                POST https://www.swcleanair.gov/permits/permitADPlist.asp
                     SelType=PLT&PlantID=<id>~<name>
              Result text has "PERMIT NO: yy-nnnn" blocks with VIEW links to
              /docs/permits/Final/<no>ADP.pdf and <no>TSD.pdf.
srcaa         Issued NOC orders are not online (records request).  The
              "Permits Under Review" page embeds an Airtable shared view of
              every NOC application since 2020 (number, source type,
              company/location, dates, status) which is fetched as JSON via
              the embed page's readSharedViewData URL.  WordPress media
              (/wp-json/wp/v2/media?search=) is also swept for PDFs.

Matching: dc_targets.entity_terms("WA") (which folds in LBL-map WA operators) +
EXTRA_TERMS below + a data-center name pattern.  Everything on Ecology's
data-centers page is kept regardless (it is a data-center list by
construction).

Output layout
-------------
  <RAW_DATA_DIR>/wa_dc_permits/
    pdfs/<agency>_<facility>_<doc>_<hash>.pdf
    wa_dc_permits_index.csv      appended row by row (resumable)
    logs/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

import dc_targets  # noqa: E402

OUTPUT_DIR = RAW_DATA_DIR / "wa_dc_permits"
INDEX_NAME = "wa_dc_permits_index.csv"

ECY_DC_PAGE = "https://ecology.wa.gov/air-climate/air-quality/data-centers"
ECY_BASE = "https://ecology.wa.gov/"
ECAP_BASE = "https://apps.ecology.wa.gov/ecap"
PSCAA_BASE = "https://pscleanair.gov"
SWCAA_BASE = "https://www.swcleanair.gov"
SRCAA_BASE = "https://spokanecleanair.org"
SRCAA_AIRTABLE_EMBED = "https://airtable.com/embed/shrOA0uf2urTH1kqR"

ALL_LANES = ["ecology_page", "ecap", "pscaa", "swcaa", "srcaa"]

INDEX_FIELDS = [
    "agency", "lane", "search_term", "facility", "city", "permit_group",
    "doc_title", "doc_date", "permit_number", "source_page", "doc_url",
    "final_url", "pdf_path", "status", "notes", "fetched_at",
]

# Operator names that appear on WA permits but not (or not usably) in
# dc_targets / the LBL map.  Generic names Ecology lists as data-center
# operators (Costco, Dell, T-Mobile, VMware, Charter, Actapio, Blackrock,
# Yahoo/Oath) are deliberately NOT search terms: Ecology's list page is kept
# whole anyway, and they would drag gas stations / cell sites out of the
# local-agency databases.
EXTRA_TERMS = [
    "NTT Data", "H5 Data", "ServerFarm", "Sabey", "Vantage Data", "CyrusOne",
    "Microsoft", "Amazon Data", "Intergate", "Digital Fortress", "Centeris",
    "Centersquare", "TierPoint", "EdgeConneX", "Equinix", "Digital Realty",
    "Lunavi", "Chirisa", "Keel Infrastructure", "Merkle Standard", "Salcido",
    "Soluna", "Cyxtera", "Colocation Northwest", "Optic Fusion",
    "Wallula Gap", "Malaga Data",
]

# dc_targets aliases too short/generic for prefix matching in WA databases.
NOISY_TERMS = {"switch", "stream data", "design llc", "skybox"}

DC_NAME_PATTERN = re.compile(
    r"data ?cent|server ?farm|colocation|co-location|hyperscale|cloud comput|"
    r"crypto|bitcoin|blockchain", re.IGNORECASE)

PERMIT_NO_RE = re.compile(r"\b\d{2}AQ-?[A-Z]\d{2,4}\s?(?:\d?R)?\b|\bNo\.?\s*\d{4,5}[A-Z]?\b|\b\d{2}-\d{4}[A-Z0-9]*\b")
TRAILING_DATE_RE = re.compile(r"\(\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}/\d{2,4})\s*\)\s*$")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/150.0 Safari/537.36",
}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 &-]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def slug(text: str, n: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text or "").strip("_")[:n]


class Matcher:
    """Return the search term (or pattern label) a facility string matches."""

    def __init__(self, terms: Iterable[str]):
        self.terms = [(t, norm(t)) for t in terms if len(norm(t)) >= 4]

    def match(self, *texts: str) -> Optional[str]:
        blob = " " + norm(" ".join(t for t in texts if t)) + " "
        if not blob.strip():
            return None
        for term, nt in self.terms:
            if f" {nt}" in blob or f"{nt} " in blob:
                return term
        if DC_NAME_PATTERN.search(blob):
            return "pattern:data-center"
        return None


class Index:
    """Row-by-row appended CSV; keyed on (agency, doc_url-or-synthetic key)."""

    def __init__(self, path: Path):
        self.path = path
        self.rows: Dict[Tuple[str, str], Dict[str, str]] = {}
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    self.rows[self.key(r)] = r
            logger.info(f"Loaded {len(self.rows)} existing index rows from {path}")
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        if path.stat().st_size == 0:
            self._w.writeheader()
            self._fh.flush()

    @staticmethod
    def key(r: Dict[str, str]) -> Tuple[str, str]:
        k = r.get("doc_url") or f"nolink:{r.get('facility','')}|{r.get('permit_group','')}|{r.get('doc_title','')}"
        return (r.get("agency", ""), k)

    def done(self, r: Dict[str, str]) -> bool:
        old = self.rows.get(self.key(r))
        if not old:
            return False
        if old.get("status") == "ok" and old.get("pdf_path") and Path(old["pdf_path"]).exists():
            return True
        return old.get("status") in ("not_posted", "listed_no_document")

    def append(self, r: Dict[str, str]) -> None:
        r = {k: r.get(k, "") for k in INDEX_FIELDS}
        r["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        self.rows[self.key(r)] = r
        self._w.writerow(r)
        self._fh.flush()

    def n_pdfs(self) -> int:
        return sum(1 for r in self.rows.values() if r.get("status") == "ok")

    def close(self) -> None:
        self._fh.close()


class Downloader:
    def __init__(self, session: requests.Session, pdf_dir: Path, delay: float,
                 timeout: int, limit: int, cap: int, index: Index, enabled: bool):
        self.session, self.pdf_dir, self.delay, self.timeout = session, pdf_dir, delay, timeout
        self.limit, self.cap, self.index, self.enabled = limit, cap, index, enabled
        self.n_new = 0
        self.n_fail = 0

    def budget_left(self) -> bool:
        if self.limit and self.n_new >= self.limit:
            return False
        return self.index.n_pdfs() < self.cap

    def fetch(self, row: Dict[str, str], headers: Optional[dict] = None) -> Dict[str, str]:
        """Download row['doc_url'] to pdfs/, fill pdf_path/final_url/status."""
        url = row.get("doc_url", "")
        if not url:
            row.setdefault("status", "not_posted")
            return row
        if not self.enabled:
            row["status"] = "indexed_only"
            return row
        if not self.budget_left():
            row["status"] = "skipped_limit"
            return row
        h = hashlib.sha1(url.encode()).hexdigest()[:8]
        stem = f"{slug(row['agency'], 12)}_{slug(row.get('facility', ''), 40)}_{slug(row.get('doc_title', ''), 50)}_{h}"
        dest = self.pdf_dir / f"{stem}.pdf"
        if dest.exists() and dest.stat().st_size > 0:
            row.update(pdf_path=str(dest), status="ok")
            return row
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True, headers=headers)
            row["final_url"] = r.url
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            if r.content[:5] == b"%PDF-":
                dest.write_bytes(r.content)
                row.update(pdf_path=str(dest), status="ok")
                self.n_new += 1
                logger.info(f"  saved {dest.name} ({len(r.content)//1024} KB)")
            elif "msword" in ctype or r.content[:2] == b"\xd0\xcf" or r.content[:2] == b"PK":
                ext = ".doc" if r.content[:2] == b"\xd0\xcf" else ".docx"
                dest = dest.with_suffix(ext)
                dest.write_bytes(r.content)
                row.update(pdf_path=str(dest), status="ok")
                self.n_new += 1
            else:
                row.update(status="not_pdf", notes=(row.get("notes", "") + f" content-type={ctype}").strip())
                logger.warning(f"  not a PDF: {url} ({ctype})")
        except Exception as e:  # noqa: BLE001
            row.update(status="error", notes=(row.get("notes", "") + f" {e}").strip()[:300])
            self.n_fail += 1
            logger.warning(f"  download failed {url}: {e}")
        time.sleep(self.delay)
        return row


def get(session: requests.Session, url: str, timeout: int, delay: float, **kw) -> requests.Response:
    r = session.get(url, timeout=timeout, **kw)
    r.raise_for_status()
    time.sleep(delay)
    return r


def base_row(agency: str, lane: str, **kw) -> Dict[str, str]:
    row = {k: "" for k in INDEX_FIELDS}
    row.update(agency=agency, lane=lane)
    row.update({k: (v or "") for k, v in kw.items()})
    return row


def process(rows: Iterable[Dict[str, str]], index: Index, dl: Downloader,
            headers: Optional[dict] = None) -> Tuple[int, int]:
    """Download + append every row not already complete. Returns (new, skipped)."""
    n_new = n_skip = 0
    for row in rows:
        if index.done(row):
            n_skip += 1
            continue
        if row.get("doc_url"):
            row = dl.fetch(row, headers=headers)
        index.append(row)
        n_new += 1
    return n_new, n_skip


# ----------------------------------------------------------------------------
# Lane 1: Ecology "Data centers" page
# ----------------------------------------------------------------------------
def ecology_doc_url(href: str) -> str:
    url = urljoin(ECY_BASE, href)
    m = re.search(r"apps\.ecology\.wa\.gov/publications/SummaryPages/([A-Za-z0-9]+)\.html", url, re.I)
    if m:
        return f"https://apps.ecology.wa.gov/publications/documents/{m.group(1)}.pdf"
    return url


def lane_ecology_page(session, matcher: Matcher, args) -> List[Dict[str, str]]:
    logger.info("[ecology_page] fetching Ecology data-centers page")
    html = get(session, ECY_DC_PAGE, args.timeout, args.delay).text
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict[str, str]] = []
    n_fac = 0
    for btn in soup.select("div.accordion-container button.accordion-title"):
        label = btn.get_text(" ", strip=True).replace("\u200b", "")
        content = btn.find_next_sibling("div", class_="accordion-content")
        if content is None:
            continue
        n_fac += 1
        parts = re.split(r"\s[-\u2013\u2014]\s", label, maxsplit=1)  # "City - Facility" / "City – Facility"
        city, facility = (parts[0], parts[1]) if len(parts) == 2 else ("", label)
        facility = facility.strip() or label
        term = matcher.match(facility, label) or "ecology-data-center-list"
        group = ""
        for el in content.find_all(["h3", "h4", "h5", "strong", "li"]):
            if el.name != "li":
                if el.find_parent("li") is None:
                    group = el.get_text(" ", strip=True).replace("\u200b", "")
                continue
            if el.find("ul") is not None:  # container li: its own text is a group label
                own = " ".join(t.strip() for t in el.find_all(string=True, recursive=False) if t.strip())
                if not own:
                    first = el.find(["a", "strong"])
                    own = first.get_text(" ", strip=True) if first else ""
                if own:
                    group = own.replace("\u200b", "")
                continue
            text = el.get_text(" ", strip=True).replace("\u200b", "")
            a = el.find("a", href=True)
            title = a.get_text(" ", strip=True) if a else text
            dm = TRAILING_DATE_RE.search(text)
            pm = PERMIT_NO_RE.search(f"{title} {group}")
            rows.append(base_row(
                "WA Ecology", "ecology_page", search_term=term, facility=facility, city=city.strip(),
                permit_group=group, doc_title=title, doc_date=dm.group(1) if dm else "",
                permit_number=pm.group(0).strip() if pm else "",
                source_page=ECY_DC_PAGE, doc_url=ecology_doc_url(a["href"]) if a else "",
                status="" if a else "not_posted",
                notes="" if a else "listed on Ecology data-centers page without a link"))
    logger.success(f"[ecology_page] {n_fac} facility accordions, {len(rows)} document rows "
                   f"({sum(1 for r in rows if r['doc_url'])} with links)")
    return rows


# ----------------------------------------------------------------------------
# Lane 2: Ecology ECAP (Title V / PSD)
# ----------------------------------------------------------------------------
def lane_ecap(session, matcher: Matcher, args) -> List[Dict[str, str]]:
    body = {"criteria": {"DocumentTypeId": 0, "IssuanceDate": {"StartDate": "", "EndDate": ""},
                         "ShowCurrentPermits": False, "DocumentMetaData": {}}}
    r = session.post(f"{ECAP_BASE}/api/documents/search-facilities/0/1000", json=body, timeout=args.timeout)
    r.raise_for_status()
    facs = r.json().get("Data", [])
    time.sleep(args.delay)
    logger.info(f"[ecap] {len(facs)} facilities in ECAP (Title V/PSD register)")
    rows: List[Dict[str, str]] = []
    for f in facs:
        term = matcher.match(f.get("Name", ""))
        if not term:
            continue
        logger.info(f"[ecap] match {f['Name']!r} ({f.get('City')}) via {term!r}")
        d = get(session, f"{ECAP_BASE}/api/documents/facility-documents/{f['Id']}/0/0/500/true",
                args.timeout, args.delay, headers={"Accept": "application/json"}).json()
        for grp in d.get("Data", []):
            for det in grp.get("Details", []):
                meta = {}
                try:
                    meta = {k: v.get("Value") for k, v in json.loads(det.get("DocumentMetaDataJson") or "{}").items()}
                except Exception:  # noqa: BLE001
                    pass
                rows.append(base_row(
                    "WA Ecology", "ecap", search_term=term, facility=f.get("Name", ""), city=f.get("City", ""),
                    permit_group=grp.get("Key", {}).get("DocumentKeyString", ""),
                    doc_title=det.get("DocumentName", ""), doc_date=(meta.get("documentDate") or "")[:10],
                    permit_number=meta.get("approvalOrderNumber") or grp.get("Key", {}).get("DocumentKeyString", ""),
                    source_page=f"{ECAP_BASE}/", doc_url=f"{ECAP_BASE}/api/documents/document-data/{det['DocumentId']}",
                    notes=f"type={det.get('DocumentType')}"))
    logger.success(f"[ecap] {len(rows)} document rows")
    return rows


# ----------------------------------------------------------------------------
# Lane 3: PSCAA (site search + comment / recent-project pages)
# ----------------------------------------------------------------------------
def pscaa_doc_links(soup) -> List[Tuple[str, str]]:
    out, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"DocumentCenter/View/\d+", re.I)):
        url = urljoin(PSCAA_BASE, a["href"]).split("?")[0]
        url = re.sub(r"^https?://(www\.)?pscleanair\.gov", PSCAA_BASE, url, flags=re.I)
        if url in seen:
            continue
        seen.add(url)
        out.append((a.get_text(" ", strip=True), url))
    return out


def lane_pscaa(session, matcher: Matcher, terms: List[str], args) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen_urls = set()
    # (a) project pages linked from Permits Open for Comment / Recent Permitting Projects
    for page in ("/175/Permits-Open-for-Comment", "/460/Recent-Permitting-Projects"):
        try:
            soup = BeautifulSoup(get(session, PSCAA_BASE + page, args.timeout, args.delay).text, "html.parser")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[pscaa] {page}: {e}")
            continue
        for h2 in soup.find_all("h2"):
            company = h2.get_text(" ", strip=True)
            term = matcher.match(company)
            if not term:
                continue
            block_links = []
            for sib in h2.find_next_siblings():
                if sib.name == "h2":
                    break
                block_links += [(a.get_text(" ", strip=True), urljoin(PSCAA_BASE, a["href"]))
                                for a in sib.find_all("a", href=True)]
            docs = [(t, u) for t, u in block_links if "DocumentCenter/View" in u]
            for t, u in block_links:
                if re.search(r"pscleanair\.gov/\d+/", u) and "DocumentCenter" not in u:
                    try:
                        sub = BeautifulSoup(get(session, u, args.timeout, args.delay).text, "html.parser")
                        docs += pscaa_doc_links(sub)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[pscaa] {u}: {e}")
            for t, u in docs:
                if u in seen_urls:
                    continue
                seen_urls.add(u)
                pm = PERMIT_NO_RE.search(t)
                rows.append(base_row("PSCAA", "pscaa", search_term=term, facility=company,
                                     doc_title=t, permit_number=pm.group(0) if pm else "",
                                     source_page=PSCAA_BASE + page, doc_url=u))
    # (b) site search per term; keep DocumentCenter hits whose title names a target
    queries = list(terms) + ["data center", "order of approval generators"]
    n_dropped = 0
    for i, q in enumerate(queries, 1):
        try:
            r = get(session, f"{PSCAA_BASE}/Search/Results", args.timeout, args.delay,
                    params={"searchPhrase": q})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[pscaa] search {q!r}: {e}")
            continue
        hits = pscaa_doc_links(BeautifulSoup(r.text, "html.parser"))
        kept = 0
        for t, u in hits:
            term = matcher.match(t)
            if not term:
                n_dropped += 1
                continue
            if u in seen_urls:
                continue
            seen_urls.add(u)
            kept += 1
            pm = PERMIT_NO_RE.search(t)
            rows.append(base_row("PSCAA", "pscaa", search_term=term, facility=t, doc_title=t,
                                 permit_number=pm.group(0) if pm else "",
                                 source_page=f"{PSCAA_BASE}/Search/Results?searchPhrase={quote(q)}", doc_url=u))
        if hits:
            logger.info(f"[pscaa] [{i}/{len(queries)}] {q!r}: {len(hits)} DocumentCenter hits, {kept} kept")
    logger.success(f"[pscaa] {len(rows)} document rows ({n_dropped} search hits dropped: no target name in title)")
    return rows


# ----------------------------------------------------------------------------
# Lane 4: SWCAA e-Permits
# ----------------------------------------------------------------------------
def lane_swcaa(session, matcher: Matcher, args) -> List[Dict[str, str]]:
    soup = BeautifulSoup(get(session, f"{SWCAA_BASE}/permits/permitADPsearch.asp", args.timeout, args.delay).text,
                         "html.parser")
    sel = soup.find("select", {"name": "PlantID"})
    plants = [(o.get("value", ""), o.get_text(" ", strip=True)) for o in sel.find_all("option")] if sel else []
    logger.info(f"[swcaa] {len(plants)} registered plants in SWCAA e-Permits")
    rows: List[Dict[str, str]] = []
    for value, name in plants:
        term = matcher.match(name)
        if not term:
            continue
        logger.info(f"[swcaa] match {name!r} via {term!r}")
        r = session.post(f"{SWCAA_BASE}/permits/permitADPlist.asp", data={"SelType": "PLT", "PlantID": value},
                         timeout=args.timeout)
        r.raise_for_status()
        time.sleep(args.delay)
        html = r.text
        permits = [(m.start(), m.group(1)) for m in re.finditer(
            r"PERMIT NO:\s*(?:<[^>]+>\s*)*([0-9]{2}-[0-9]{3,4}[A-Z0-9]*)", html)]
        finals = [(m.start(), m.group(1)) for m in re.finditer(r"Date Final:\s*(?:<[^>]+>\s*)*([A-Za-z]+,\s*[A-Za-z]+ \d{1,2}, \d{4})", html)]
        for m in re.finditer(r"<a[^>]+href=\"([^\"]*docs/(?:permits|SEPA)[^\"]+)\"[^>]*>", html, re.I):
            pos, href = m.start(), m.group(1)
            permit = next((p for s, p in reversed(permits) if s < pos), "")
            fdate = next((d for s, d in finals if s > pos), "")
            url = urljoin(f"{SWCAA_BASE}/permits/", href)
            kind = "ADP" if re.search(r"ADP\.pdf$", href, re.I) else ("TSD" if re.search(r"TSD\.pdf$", href, re.I) else Path(href).stem)
            rows.append(base_row("SWCAA", "swcaa", search_term=term, facility=name, permit_group=permit,
                                 doc_title=f"{permit} {kind}".strip(), doc_date=fdate, permit_number=permit,
                                 source_page=f"{SWCAA_BASE}/permits/permitADPlist.asp?PlantID={quote(value)}",
                                 doc_url=url))
    logger.success(f"[swcaa] {len(rows)} document rows")
    return rows


# ----------------------------------------------------------------------------
# Lane 5: SRCAA (Airtable NOC application list + WordPress media)
# ----------------------------------------------------------------------------
def lane_srcaa(session, matcher: Matcher, terms: List[str], args) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    # (a) Airtable shared view of NOC applications (2020 to date)
    try:
        page = get(session, SRCAA_AIRTABLE_EMBED, args.timeout, args.delay,
                   params={"backgroundColor": "blue", "viewControls": "on"}).text
        m = re.search(r'urlWithParams: "([^"]+)"', page)
        app = re.search(r'"applicationId":"(app[A-Za-z0-9]+)"', page)
        if not m:
            raise RuntimeError("readSharedViewData URL not found in embed page")
        url = "https://airtable.com" + m.group(1).encode().decode("unicode_escape")
        hdrs = {"x-airtable-application-id": app.group(1) if app else "", "x-airtable-inter-service-client": "webClient",
                "x-requested-with": "XMLHttpRequest", "x-time-zone": "America/Los_Angeles", "x-user-locale": "en"}
        d = get(session, url, args.timeout, args.delay, headers=hdrs).json()["data"]["table"]
        cols = {c["id"]: c for c in d["columns"]}
        def cell(row, name):
            for cid, c in cols.items():
                if c["name"] == name:
                    v = row.get("cellValuesByColumnId", {}).get(cid)
                    if c["type"] == "select" and v:
                        return c.get("typeOptions", {}).get("choices", {}).get(v, {}).get("name", v)
                    return (v or "")[:10] if c["type"] == "date" and v else (v or "")
            return ""
        n = 0
        for r in d["rows"]:
            company, stype, num = cell(r, "Company/Location"), cell(r, "Source Type"), cell(r, "Number")
            term = matcher.match(company)
            if not term:
                continue
            n += 1
            fac, _, addr = company.partition("/")
            rows.append(base_row(
                "SRCAA", "srcaa_airtable", search_term=term, facility=fac.strip(), city="Spokane County",
                permit_group=f"NOC {num}", doc_title=f"NOC application {num}: {stype}",
                doc_date=cell(r, "Date Posted"), permit_number=num,
                source_page=f"{SRCAA_BASE}/about/permits-under-review/", doc_url="",
                status="listed_no_document",
                notes=f"address={addr.strip()}; status={cell(r, 'Status')}; approved={cell(r, 'Date Approved')}; "
                      f"documents by public records request"))
        logger.info(f"[srcaa] Airtable: {len(d['rows'])} NOC applications since 2020, {n} match targets")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[srcaa] Airtable list failed: {e}")
    # (b) WordPress media search
    seen = set()
    for q in list(terms) + ["data center", "order of approval"]:
        try:
            items = get(session, f"{SRCAA_BASE}/wp-json/wp/v2/media", args.timeout, args.delay,
                        params={"search": q, "per_page": 50}).json()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[srcaa] media search {q!r}: {e}")
            continue
        for it in items if isinstance(items, list) else []:
            if "pdf" not in (it.get("mime_type") or ""):
                continue
            title = BeautifulSoup(it.get("title", {}).get("rendered", ""), "html.parser").get_text()
            url = it.get("source_url", "")
            term = matcher.match(title, Path(url).name)
            if not term or url in seen:
                continue
            seen.add(url)
            rows.append(base_row("SRCAA", "srcaa_wp_media", search_term=term, facility=title, doc_title=title,
                                 doc_date=(it.get("date") or "")[:10],
                                 source_page=f"{SRCAA_BASE}/wp-json/wp/v2/media?search={quote(q)}", doc_url=url))
    logger.success(f"[srcaa] {len(rows)} rows ({sum(1 for r in rows if r['doc_url'])} with PDFs)")
    return rows


# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lanes", default=",".join(ALL_LANES), help=f"Comma-separated subset of {ALL_LANES}")
    ap.add_argument("--terms", default="", help="Comma-separated search terms (overrides dc_targets)")
    ap.add_argument("--limit", type=int, default=0, help="Max NEW PDF downloads this run (0 = unlimited)")
    ap.add_argument("--cap", type=int, default=1000, help="Max total PDFs in the output dir")
    ap.add_argument("--no-download", action="store_true", help="Index only")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    (OUTPUT_DIR / "logs").mkdir(exist_ok=True)
    logger.add(OUTPUT_DIR / "logs" / f"run_{datetime.now():%Y%m%d_%H%M%S}.log", level="DEBUG")

    lanes = [l.strip() for l in args.lanes.split(",") if l.strip()]
    bad = [l for l in lanes if l not in ALL_LANES]
    if bad:
        ap.error(f"unknown lanes {bad}; choose from {ALL_LANES}")

    if args.terms:
        terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    else:
        # entity_terms() already folds in every LBL-map WA company/operator;
        # the map's facility_name column is mostly building codes (CO5,
        # MWH01, SEA2-A) so it is not used as search terms.
        terms = dc_targets.entity_terms("WA", include_facilities=False) + EXTRA_TERMS
    seen, uniq = set(), []
    for t in terms:
        if norm(t) and norm(t) not in seen and len(norm(t)) >= 4 and norm(t) not in NOISY_TERMS:
            seen.add(norm(t))
            uniq.append(t)
    terms = uniq
    matcher = Matcher(terms)
    logger.info(f"{len(terms)} target terms; lanes={lanes}; limit={args.limit or 'none'}; cap={args.cap}")

    session = requests.Session()
    session.headers.update(HEADERS)
    index = Index(OUTPUT_DIR / INDEX_NAME)
    dl = Downloader(session, pdf_dir, args.delay, args.timeout, args.limit, args.cap, index, not args.no_download)

    summary = {}
    for lane in lanes:
        try:
            if lane == "ecology_page":
                rows = lane_ecology_page(session, matcher, args)
            elif lane == "ecap":
                rows = lane_ecap(session, matcher, args)
            elif lane == "pscaa":
                rows = lane_pscaa(session, matcher, terms, args)
            elif lane == "swcaa":
                rows = lane_swcaa(session, matcher, args)
            else:
                rows = lane_srcaa(session, matcher, terms, args)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[{lane}] lane failed: {e}")
            summary[lane] = "FAILED"
            continue
        n_new, n_skip = process(rows, index, dl,
                                headers={"Accept": "application/pdf,*/*"} if lane == "ecap" else None)
        summary[lane] = f"{len(rows)} rows, {n_new} appended, {n_skip} already complete"
        logger.success(f"[{lane}] {summary[lane]}")
        if dl.limit and dl.n_new >= dl.limit:
            logger.warning(f"--limit {dl.limit} reached; remaining rows indexed as skipped_limit")

    index.close()
    logger.success(f"Run complete: {dl.n_new} new PDFs, {dl.n_fail} failures; "
                   f"index has {len(index.rows)} rows / {index.n_pdfs()} PDFs -> {OUTPUT_DIR / INDEX_NAME}")
    for lane, s in summary.items():
        logger.info(f"  {lane}: {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
