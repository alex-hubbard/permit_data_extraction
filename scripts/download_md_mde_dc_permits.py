#!/usr/bin/env python3
"""Download Maryland MDE air-permit documents for data centers.

Maryland data-center generator fleets hold Permits to Construct (PTC) and
state / synthetic-minor Permits to Operate rather than Title V, and MDE's Air
and Radiation Administration (ARA) publishes them in several places, none of
which is a searchable permit database.  This script sweeps every public lane
found on 2026-09-02 and keeps the documents that name a data-center operator,
a known data-center address, or a large emergency-generator fleet:

amp_live / amp_wayback
    https://mde.maryland.gov/programs/permits/AirManagementPermits/Pages/index.aspx
    "Public Participation Opportunities": SharePoint list web parts for
    informational-meeting notices, public hearings, final determinations,
    Title V hearings and the ALTERNATE NSPS PUBLIC PARTICIPATION list (the
    large-generator data-center applications: Aligned IAD04, Amazon BWI150,
    Amazon IAD534 ...).  The lists are server-rendered as
    ``var WPQnListData = {"Row": [...]}`` JSON (fields Company_x0020_Address,
    Project, Attachment / Docket_x0020__x0023_ / Public_x0020_Notice = site
    relative PDF paths under /programs/permits/AirManagementPermits/Documents/
    Public-Review/...) and, in some archived renderings, as plain HTML tables.
    Only current actions stay on the page, so Wayback Machine snapshots (CDX
    API, one per distinct digest) are parsed the same way; documents are
    fetched live first and from the archive (``/web/<ts>id_/<url>``) when the
    live copy is gone.  SharePoint folder listings / REST (_api, Forms/
    AllItems.aspx) answer 401 to anonymous users, so lists are the only index.

cdx_docs
    Wayback CDX listing of everything ever archived under
    .../AirManagementPermits/Documents/Public-Review/* (Alt NSPS folder etc.),
    filtered by file name — catches documents whose list rows were never
    captured.

dc_pages
    https://mde.maryland.gov/datacenters/Pages/index.aspx and
    .../datacenters/Pages/FrederickDataCenter.aspx — MDE's data-center hub
    (Quantum Frederick / Eastalco campus): issued PTCs and final
    determinations for Amazon Data Services (BWI150) and Aligned, plus
    brownfield / water documents (indexed, downloaded only with --dc-page-all).

ara
    Socrata dataset r8cv-wcdu "MDE - ARA Permits" (opendata.maryland.gov,
    459 state + Part 70 permits to operate).  Each row's ``document`` URL is
    https://mdedataviewer.mde.state.md.us/OpenDataDocuments?ID=<n>, which
    lists the permit files only when the request carries an
    opendata.maryland.gov Referer; the files themselves come from
    /DownloadFileHandler.ashx?ID=<fileId> with the listing page's session
    cookie.  (Amazon LTW060/061/062, Microsoft "Yellow Facility", Prosperity
    Drive Data Center, Fannie Mae UTC Data Center ...)

notices
    Socrata dataset 3iva-5cca "MDE - Public Notice" (the HB1200 permit
    application database, media_type = Air Quality Permits) — status /
    dates only, no document links; matched rows are indexed with
    status=index_only.

t5_draft
    .../Pages/title5draftpermits.aspx — draft Part 70 dockets (rarely data
    centers; included for completeness).

Matching (recorded in ``match_reason``): dc_targets entity terms for MD
plus MD-specific aliases, LBL data-center-map MD facility names and street
addresses, a data-center name pattern (data center, IAD###/BWI###/LTW###
site codes ...) and a generator-fleet heuristic (>= --gen-min generators in
the project description).

Output layout
-------------
  <RAW_DATA_DIR>/md_mde_dc_permits/
    pdfs/<lane>_<facility>_<file>.pdf
    md_mde_dc_permits_index.csv          one row per document, appended as we
                                         go (last row per doc_url wins)
    md_mde_public_review_rows_full.csv   every list row seen (all facilities)
    md_mde_ara_permits_full.csv          every ARA permit row with DC flags
    md_mde_hb1200_air_notices.csv        every HB1200 air-permit notice row
"""

from __future__ import annotations

import argparse
import csv
import html as htmlmod
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

import dc_targets  # noqa: E402

BASE = "https://mde.maryland.gov"
AMP_INDEX_PATH = "/programs/permits/AirManagementPermits/Pages/index.aspx"
AMP_INDEX_URL = BASE + AMP_INDEX_PATH
DC_PAGES = ["/datacenters/Pages/index.aspx", "/datacenters/Pages/FrederickDataCenter.aspx"]
T5_DRAFT_URL = BASE + "/programs/permits/AirManagementPermits/Pages/title5draftpermits.aspx"
PUBLIC_REVIEW_PREFIX = "mde.maryland.gov/programs/permits/AirManagementPermits/Documents/Public-Review/"
SODA_ARA = "https://opendata.maryland.gov/resource/r8cv-wcdu.json"
SODA_NOTICE = "https://opendata.maryland.gov/resource/3iva-5cca.json"
OPENDATA_REFERER = "https://opendata.maryland.gov/"
DATAVIEWER = "https://mdedataviewer.mde.state.md.us"
CDX = "http://web.archive.org/cdx/search/cdx"
WAYBACK_RAW = "https://web.archive.org/web/{ts}id_/{url}"

OUTPUT_DIR = RAW_DATA_DIR / "md_mde_dc_permits"
INDEX_NAME = "md_mde_dc_permits_index.csv"
ALL_SOURCES = ["dc_pages", "amp_live", "ara", "cdx_docs", "amp_wayback", "notices", "t5_draft"]

INDEX_FIELDS = [
    "source", "section", "snapshot", "site_id", "facility", "address", "project",
    "permit_type", "permit_status", "permit_date", "doc_label", "doc_url",
    "archive_url", "match_reason", "is_dc", "status", "pdf_path", "bytes", "fetched_at",
]
ROWS_FIELDS = ["source", "snapshot", "section", "item_id", "facility", "project",
               "doc_label", "doc_url", "dates", "is_dc", "match_reason"]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}

# Maryland operators / campus names that are not (or not yet) in dc_targets.
MD_EXTRA_ALIASES = [
    "Quantum Loophole", "Quantum Frederick", "Quantum Maryland", "Catellus",
    "Atmosphere DC", "Aligned Data Centers", "Align Data Centers", "Rowan",
    "Bauxite", "TPG", "Cloud HQ", "Amazon Data Services", "Fannie Mae",
    "Propco", "Data Services",
]
# Known Maryland data-center street addresses beyond the LBL map.
MD_EXTRA_ADDRESSES = [
    "3300 Radiant Way", "3250 Digital Drive", "5601 Manor Woods Road",
    "610 Guardian Way", "7885 Sellner Rd", "8201 Dorsey Run Rd",
    "12401 Prosperity Drive", "9107 Bennett Creek Blvd", "21230 Martinsburg Road",
]
DC_NAME_RE = re.compile(
    r"data\s*cent|datacenter|colocation|co-location|hyperscale|server farm|"
    r"\b(?:iad|bwi|ltw|dca)\s?\d{2,3}\b", re.I)
AIR_DOC_RE = re.compile(r"air permit|air quality|\bptc\b|permit to construct|final determination|"
                        r"response to comments|generator", re.I)
GENERIC_FACILITY_RE = re.compile(r"^(baltimore|silver spring|old |bal\d|frederick$|laurel$|dc-\d)", re.I)


# --------------------------------------------------------------------------- helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 &-]", " ", str(s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def strip_html(s: str) -> str:
    s = htmlmod.unescape(re.sub(r"<br\s*/?>", " ", str(s or ""), flags=re.I))
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def address_key(addr: str) -> Optional[str]:
    """'3250 Digital Drive, Bldg B' -> '3250 digital' for fuzzy address joins."""
    a = re.sub(r"[^a-z0-9 ]", " ", str(addr or "").lower())
    a = re.sub(r"\b(north|south|east|west)\b", lambda m: m.group(1)[0], a)
    a = re.sub(r"\b(street|st|road|rd|drive|dr|avenue|ave|parkway|pkwy|lane|ln|boulevard|"
               r"blvd|pike|way|highway|hwy|court|ct|place|pl|suite|ste|circle|cir|loop|lp)\b", " ", a)
    m = re.match(r"\s*(\d+)\s+([a-z0-9]+)", a)
    return f"{m.group(1)} {m.group(2)}" if m else None


def text_address_keys(text: str) -> Set[str]:
    a = re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower())
    a = re.sub(r"\b(north|south|east|west)\b", lambda m: m.group(1)[0], a)
    return {f"{n} {w}" for n, w in re.findall(r"\b(\d{2,6})\s+([a-z][a-z0-9]+)", a)}


def generator_count(text: str) -> int:
    """Largest unit count that precedes the word 'generator' (ratings excluded)."""
    best = 0
    for m in re.finditer(r"generator", text or "", re.I):
        window = text[max(0, m.start() - 80):m.start()]
        for n in re.finditer(r"\b(\d{1,4})\b", window):
            between = window[n.end():]
            if re.search(r"\d|kw|kilowatt|mw|megawatt|hp|horsepower|bhp|%|hour", between, re.I):
                continue
            v = int(n.group(1))
            if 2 <= v <= 2000:
                best = max(best, v)
    return best


def fix_url(href: str, base: str) -> str:
    u = htmlmod.unescape((href or "").strip()).replace("​", "")
    u = re.sub(r"^http://(?=https?://)", "", u)          # 'http://https://mde...' typos
    u = re.sub(r"https?://$", "", u)                      # '....pdfhttp://' typos
    if not u or u.lower().startswith(("javascript", "mailto", "#")):
        return ""
    u = urljoin(base, u)
    if "web.archive.org/web/" in u:                       # archived page with rewritten links
        u = re.sub(r"^https?://web\.archive\.org/web/\d+(?:id_|im_|if_)?/", "", u)
    return u


def canon_url(u: str) -> str:
    p = urlparse(u)
    path = quote(unquote(p.path), safe="/-_.~%()")
    return f"{p.scheme or 'https'}://{p.netloc.lower()}{path}"


def slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(s or "")).strip("_")
    return s[:n].rstrip("_") or "x"


def ext_from(resp: requests.Response, content: bytes) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    if m and "." in m.group(1):
        ext = unquote(m.group(1)).rsplit(".", 1)[1].lower().strip()
        if re.fullmatch(r"[a-z0-9]{2,5}", ext):
            return ext
    if content[:4] == b"%PDF":
        return "pdf"
    if content[:2] == b"PK":
        return "docx"
    if content[:4] == b"\xd0\xcf\x11\xe0":
        return "doc"
    return ""


def is_document(content: bytes) -> bool:
    return content[:4] == b"%PDF" or content[:2] == b"PK" or content[:4] == b"\xd0\xcf\x11\xe0"


class Http:
    def __init__(self, delay: float, timeout: int):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay, self.timeout = delay, timeout
        self._last = 0.0

    def get(self, url: str, retries: int = 3, timeout: Optional[int] = None,
            polite: Optional[float] = None, **kw) -> requests.Response:
        wait = self.delay if polite is None else polite
        gap = time.time() - self._last
        if gap < wait:
            time.sleep(wait - gap)
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                r = self.s.get(url, timeout=timeout or self.timeout, allow_redirects=True, **kw)
                self._last = time.time()
                if r.status_code in (408, 429, 500, 502, 503, 504):
                    time.sleep(min(20, 2 ** (attempt + 1)))
                    last = RuntimeError(f"HTTP {r.status_code}")
                    continue
                return r
            except requests.RequestException as e:
                last = e
                self._last = time.time()
                time.sleep(min(20, 2 ** (attempt + 1)))
        raise last or RuntimeError(f"request failed: {url}")


# --------------------------------------------------------------------------- matching
class DCMatcher:
    def __init__(self, extra_terms: Iterable[str] = (), gen_min: int = 10):
        self.gen_min = gen_min
        terms = list(dc_targets.entity_terms("MD")) + MD_EXTRA_ALIASES + list(extra_terms)
        self.long_terms = sorted({norm(t) for t in terms if len(norm(t)) > 4}, key=len, reverse=True)
        short = sorted({norm(t) for t in terms if 0 < len(norm(t)) <= 4})
        self.short_re = re.compile("|".join(r"\b" + re.escape(t) + r"\b" for t in short)) if short else None
        df = dc_targets.load_combined()
        md = df[df["state"].astype(str).str.upper() == "MD"] if len(df) else df
        self.addr_keys: Dict[str, str] = {}
        for _, r in md.iterrows():
            k = address_key(r.get("address", ""))
            if k:
                self.addr_keys.setdefault(k, str(r.get("facility_name", "")))
        for a in MD_EXTRA_ADDRESSES:
            k = address_key(a)
            if k:
                self.addr_keys.setdefault(k, a)
        generic = {"md", "data", "center", "centers", "campus", "building", "customer", "county",
                   "primary", "former", "old", "i", "ii", "iii", "iv", "one", "market", "gateway",
                   "premier", "site", "dc"}
        for c in md.get("city", []):
            generic.update(norm(c).split())
        self.facility_names = sorted({
            norm(n) for n in md.get("facility_name", []) if isinstance(n, str)
            and len(norm(n)) >= 10 and " " in norm(n) and not GENERIC_FACILITY_RE.match(n)
            and not all(t in generic or re.fullmatch(r"[a-z]?\d+[a-z]?", t) for t in norm(n).split())
        }, key=len, reverse=True)
        logger.info(f"DC matcher: {len(self.long_terms)} long terms, {len(short)} short terms, "
                    f"{len(self.addr_keys)} addresses, {len(self.facility_names)} facility names")

    def match(self, *texts: str) -> str:
        raw = " | ".join(t for t in texts if t)
        text = norm(raw)
        reasons: List[str] = []
        if DC_NAME_RE.search(raw):
            reasons.append("dc_name")
        hit = next((t for t in self.long_terms if t in text), None)
        if hit:
            reasons.append(f"term:{hit}")
        if self.short_re:
            m = self.short_re.search(text)
            if m:
                reasons.append(f"term:{m.group(0)}")
        keys = text_address_keys(raw) & set(self.addr_keys)
        if keys:
            reasons.append("address:" + sorted(keys)[0])
        fac = next((f for f in self.facility_names if f in text), None)
        if fac:
            reasons.append(f"facility:{fac}")
        n = generator_count(raw)
        if n >= self.gen_min:
            reasons.append(f"generator_fleet:{n}")
        return "; ".join(reasons)


# --------------------------------------------------------------------------- index
class Index:
    """Append-only CSV index; documents already on disk are skipped on resume."""

    def __init__(self, path: Path):
        self.path = path
        self.done: Set[str] = set()
        self.seen: Set[str] = set()
        self.n_ok = 0
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    key = r.get("doc_url", "")
                    self.seen.add(key)
                    if r.get("status") == "ok" and r.get("pdf_path") and Path(r["pdf_path"]).exists():
                        if key not in self.done:
                            self.n_ok += 1
                        self.done.add(key)
            logger.info(f"Index has {len(self.seen)} rows, {len(self.done)} documents on disk")
        else:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=INDEX_FIELDS).writeheader()
        self.attempted: Set[str] = set()

    def append(self, row: Dict[str, str]) -> None:
        row = {k: str(row.get(k, "") or "") for k in INDEX_FIELDS}
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=INDEX_FIELDS).writerow(row)
        self.seen.add(row["doc_url"])
        if row["status"] == "ok" and row["doc_url"] not in self.done:
            self.done.add(row["doc_url"])
            self.n_ok += 1


class RowsLog:
    """Every list row we parse (all facilities) — append-only, deduped."""

    def __init__(self, path: Path):
        self.path = path
        self.seen: Set[Tuple[str, str, str]] = set()
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    self.seen.add((r["facility"], r["project"], r["doc_url"]))
        else:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=ROWS_FIELDS).writeheader()

    def add(self, row: Dict[str, str]) -> bool:
        key = (row.get("facility", ""), row.get("project", ""), row.get("doc_url", ""))
        if key in self.seen:
            return False
        self.seen.add(key)
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=ROWS_FIELDS).writerow({k: row.get(k, "") for k in ROWS_FIELDS})
        return True


# --------------------------------------------------------------------------- download
def wayback_latest(http: Http, url: str) -> Optional[str]:
    """Most recent archived 200 copy of a URL (raw), or None."""
    try:
        r = http.get(CDX, params={"url": url, "fl": "timestamp,original", "filter": "statuscode:200",
                                  "limit": "-3"}, timeout=90, polite=2.0)
        lines = [ln.split(" ", 1) for ln in r.text.strip().splitlines() if " " in ln]
        if lines:
            ts, orig = lines[-1]
            return WAYBACK_RAW.format(ts=ts, url=orig)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"CDX lookup failed for {url}: {e}")
    return None


def fetch_document(http: Http, url: str, dest_stem: Path, args, headers: Optional[Dict] = None,
                   allow_wayback: bool = True) -> Tuple[str, str, int, str]:
    """Returns (status, path, bytes, archive_url). Live first, archive fallback."""
    tried: List[str] = []
    candidates: List[Tuple[str, bool]] = [(url, False)]
    for cand, is_archive in candidates:
        try:
            r = http.get(cand, timeout=args.timeout * 3, headers=headers or {},
                         polite=2.0 if is_archive else None)
            content = r.content
            if r.status_code == 200 and is_document(content):
                ext = ext_from(r, content) or "pdf"
                dest = dest_stem.with_suffix("." + ext)
                dest.write_bytes(content)
                return "ok", str(dest), len(content), (cand if is_archive else "")
            tried.append(f"{'archive' if is_archive else 'live'}:{r.status_code}:"
                         f"{r.headers.get('Content-Type', '')[:30]}")
        except Exception as e:  # noqa: BLE001
            tried.append(f"{'archive' if is_archive else 'live'}:{type(e).__name__}")
        if not is_archive and allow_wayback:
            arch = wayback_latest(http, url)
            if arch:
                candidates.append((arch, True))
    return "failed:" + "|".join(tried)[:120], "", 0, ""


def download_row(http: Http, index: Index, budget: Dict[str, int], row: Dict[str, str],
                 dest_stem: Path, args, headers: Optional[Dict] = None, allow_wayback: bool = True) -> None:
    key = row["doc_url"]
    if key in index.done or key in index.attempted:
        return
    if args.no_download:
        if key not in index.seen:
            index.append({**row, "status": "index_only", "fetched_at": now_iso()})
        return
    if budget["left"] <= 0:
        if key not in index.seen:
            index.append({**row, "status": "pending:budget", "fetched_at": now_iso()})
        return
    index.attempted.add(key)
    status, path, nbytes, archive_url = fetch_document(http, key, dest_stem, args, headers, allow_wayback)
    if status == "ok":
        budget["left"] -= 1
    index.append({**row, "status": status, "pdf_path": path, "bytes": nbytes,
                  "archive_url": archive_url or row.get("archive_url", ""), "fetched_at": now_iso()})
    (logger.info if status == "ok" else logger.warning)(
        f"  {status} [{budget['left']} left] {row['facility'][:40]} — {row['doc_label'][:40]} "
        f"{Path(path).name if path else key[-60:]}")


# --------------------------------------------------------------------------- AMP index page parsing
def _headings(src: str) -> List[Tuple[int, str]]:
    out = []
    for m in re.finditer(r"<h[23][^>]*>(.*?)</h[23]>", src, re.S | re.I):
        t = strip_html(m.group(1))
        if t:
            out.append((m.start(), t))
    return out


def _section_at(pos: int, heads: List[Tuple[int, str]]) -> str:
    sec = ""
    for p, t in heads:
        if p < pos:
            sec = t
        else:
            break
    return sec[:80]


def parse_amp_page(src: str, page_url: str) -> List[Dict[str, str]]:
    """List rows from the AMP index page: JSON web-part blobs + HTML tables."""
    rows: List[Dict[str, str]] = []
    heads = _headings(src)
    seen: Set[Tuple[str, str, str]] = set()

    def add(section, item_id, facility, project, docs, dates):
        for label, url in docs or [("", "")]:
            key = (facility, project, url)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"section": section, "item_id": item_id, "facility": facility,
                         "project": project, "doc_label": label, "doc_url": url, "dates": dates})

    for m in re.finditer(r"var (WPQ\d+)ListData = (\{.*?\});\s*var \1SchemaData", src, re.S):
        try:
            data = json.loads(m.group(2))
        except json.JSONDecodeError as e:
            logger.debug(f"{m.group(1)}: bad JSON ({e})")
            continue
        section = _section_at(m.start(), heads)
        for r in data.get("Row", []):
            facility = strip_html(r.get("Company_x0020_Address") or r.get("Name") or r.get("Title") or "")
            project = strip_html(r.get("Project") or r.get("Project_x0020_Description") or "")
            docs, dates = [], []
            for k, v in r.items():
                if not isinstance(v, str) or k.endswith((".desc", ".FriendlyDisplay")):
                    continue
                if "/Documents/" in v or v.lower().endswith(".pdf"):
                    url = fix_url(v, page_url)
                    if url:
                        docs.append((strip_html(r.get(k + ".desc", "")) or strip_html(k.replace("_x0020_", " ")), url))
                elif re.search(r"date|deadline|expir|modified|period", k, re.I) and v.strip():
                    dates.append(f"{strip_html(k.replace('_x0020_', ' ').replace('_x0023_', '#'))}={strip_html(v)}")
            add(section, str(r.get("ID", "")), facility, project, docs, "; ".join(dates))

    soup = BeautifulSoup(src, "html.parser")
    for tr in soup.find_all("tr"):
        anchors = [a for a in tr.find_all("a", href=True)
                   if "/Documents/" in a["href"] or a["href"].lower().endswith(".pdf")]
        if not anchors:
            continue
        cells = tr.find_all(["td", "th"], recursive=False)
        texts = [strip_html(str(c)) for c in cells]
        if len(texts) < 2 or tr.find("tr"):
            continue  # header rows / nested layout tables
        facility, project = texts[0], texts[1]
        docs = [(strip_html(a.get_text(" ")), fix_url(a["href"], page_url)) for a in anchors]
        docs = [(l, u) for l, u in docs if u]
        link_texts = {strip_html(a.get_text(" ")) for a in anchors}
        dates = "; ".join(t for t in texts[2:] if t and t not in link_texts)
        h = tr.find_previous(["h3", "h2"])
        add(strip_html(str(h))[:80] if h else "", "", facility, project, docs, dates)
    return rows


def process_amp_rows(http, index, rows_log, matcher, budget, args, rows, source, snapshot, pdf_dir):
    n_dc = 0
    for r in rows:
        reason = matcher.match(r["facility"], r["project"], r["doc_label"], Path(unquote(r["doc_url"])).name)
        is_dc = bool(reason)
        rows_log.add({**r, "source": source, "snapshot": snapshot, "is_dc": int(is_dc), "match_reason": reason})
        if not is_dc or not r["doc_url"]:
            continue
        n_dc += 1
        fname = unquote(Path(urlparse(r["doc_url"]).path).name)
        stem = pdf_dir / f"amp_{slug(r['facility'].split(',')[0], 30)}_{slug(Path(fname).stem, 70)}"
        download_row(http, index, budget, {
            "source": source, "section": r["section"], "snapshot": snapshot, "site_id": r["item_id"],
            "facility": r["facility"], "address": "", "project": r["project"], "permit_type": "Air PTC public review",
            "permit_status": "", "permit_date": r["dates"], "doc_label": r["doc_label"],
            "doc_url": canon_url(r["doc_url"]) if r["doc_url"].startswith("http") else r["doc_url"],
            "match_reason": reason, "is_dc": 1,
        }, stem, args)
    return n_dc


def lane_amp_live(http, index, rows_log, matcher, budget, args, pdf_dir):
    r = http.get(AMP_INDEX_URL)
    r.raise_for_status()
    rows = parse_amp_page(r.text, AMP_INDEX_URL)
    n_dc = process_amp_rows(http, index, rows_log, matcher, budget, args, rows, "amp_live", "live", pdf_dir)
    logger.success(f"amp_live: {len(rows)} list rows, {n_dc} data-center document rows")


def cdx_snapshots(http, url: str, since: str) -> List[str]:
    r = http.get(CDX, params={"url": url, "fl": "timestamp,digest", "filter": "statuscode:200",
                              "collapse": "digest", "from": since}, timeout=120, polite=2.0)
    return [ln.split()[0] for ln in r.text.strip().splitlines() if ln.strip()]


def lane_amp_wayback(http, index, rows_log, matcher, budget, args, pdf_dir):
    stamps = cdx_snapshots(http, AMP_INDEX_URL, args.wayback_since)
    if args.wayback_max:
        stamps = stamps[-args.wayback_max:]
    logger.info(f"amp_wayback: {len(stamps)} snapshots since {args.wayback_since}")
    total_rows = total_dc = 0
    for i, ts in enumerate(stamps, 1):
        url = WAYBACK_RAW.format(ts=ts, url=AMP_INDEX_URL)
        try:
            r = http.get(url, timeout=120, polite=max(args.delay, 2.0))
            if r.status_code != 200:
                logger.warning(f"[{i}/{len(stamps)}] {ts}: HTTP {r.status_code}")
                continue
            rows = parse_amp_page(r.text, AMP_INDEX_URL)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{i}/{len(stamps)}] {ts}: {e}")
            continue
        n_dc = process_amp_rows(http, index, rows_log, matcher, budget, args, rows, "amp_wayback", ts, pdf_dir)
        total_rows += len(rows)
        total_dc += n_dc
        logger.info(f"[{i}/{len(stamps)}] {ts}: {len(rows)} rows, {n_dc} DC rows")
    logger.success(f"amp_wayback: {total_rows} rows, {total_dc} DC document rows over {len(stamps)} snapshots")


# --------------------------------------------------------------------------- Public-Review CDX enumeration
def lane_cdx_docs(http, index, rows_log, matcher, budget, args, pdf_dir):
    r = http.get(CDX, params={"url": PUBLIC_REVIEW_PREFIX + "*", "fl": "timestamp,original",
                              "filter": "statuscode:200", "collapse": "urlkey"}, timeout=120, polite=2.0)
    docs = [ln.split(" ", 1) for ln in r.text.strip().splitlines() if " " in ln]
    n_dc = 0
    for ts, orig in docs:
        orig = orig.split("?")[0]
        fname = unquote(Path(urlparse(orig).path).name)
        folder = unquote(urlparse(orig).path.split("Public-Review/")[-1].rsplit("/", 1)[0]) if "/" in urlparse(orig).path.split("Public-Review/")[-1] else ""
        reason = matcher.match(fname.replace("_", " ").replace("-", " "))
        rows_log.add({"source": "cdx_docs", "snapshot": ts, "section": folder, "item_id": "", "facility": "",
                      "project": "", "doc_label": fname, "doc_url": canon_url(orig), "dates": "",
                      "is_dc": int(bool(reason)), "match_reason": reason})
        if not reason:
            continue
        n_dc += 1
        stem = pdf_dir / f"amp_{slug(folder.split('/')[-1], 20)}_{slug(Path(fname).stem, 70)}"
        download_row(http, index, budget, {
            "source": "cdx_docs", "section": folder, "snapshot": ts, "site_id": "", "facility": "",
            "address": "", "project": "", "permit_type": "Air PTC public review", "permit_status": "",
            "permit_date": "", "doc_label": fname, "doc_url": canon_url(orig), "match_reason": reason, "is_dc": 1,
        }, stem, args)
    logger.success(f"cdx_docs: {len(docs)} archived Public-Review documents, {n_dc} data-center matches")


# --------------------------------------------------------------------------- MDE data-center pages
def lane_dc_pages(http, index, rows_log, matcher, budget, args, pdf_dir):
    n = 0
    for path in DC_PAGES:
        url = BASE + path
        r = http.get(url)
        if r.status_code != 200:
            logger.warning(f"dc_pages: {url} HTTP {r.status_code}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = fix_url(a["href"], url)
            if not href or "mde.maryland.gov" not in href:
                continue
            if not re.search(r"\.(pdf|docx?|xlsx?)$", urlparse(href).path, re.I):
                continue
            label = strip_html(a.get_text(" ")) or unquote(Path(urlparse(href).path).name)
            fname = unquote(Path(urlparse(href).path).name)
            reason = matcher.match(label, fname.replace("_", " "))
            air = bool(AIR_DOC_RE.search(label + " " + fname))
            m = re.match(r"(Amazon Data Services|Aligned Data Centers|Rowan|Quantum \w+|Catellus|Alcoa)", label)
            facility = m.group(1) if m else label.split(" ")[0]
            rows_log.add({"source": "dc_pages", "snapshot": "live", "section": path, "item_id": "",
                          "facility": facility, "project": label, "doc_label": label,
                          "doc_url": canon_url(href), "dates": "", "is_dc": 1, "match_reason": reason or "dc_page"})
            if not (air or args.dc_page_all):
                continue
            n += 1
            stem = pdf_dir / f"dcpage_{slug(facility, 25)}_{slug(Path(fname).stem, 70)}"
            download_row(http, index, budget, {
                "source": "dc_pages", "section": path, "snapshot": "live", "site_id": "", "facility": facility,
                "address": "", "project": label, "permit_type": "Air PTC (MDE data-center page)" if air else "other",
                "permit_status": "", "permit_date": "", "doc_label": label, "doc_url": canon_url(href),
                "match_reason": reason or "dc_page", "is_dc": 1,
            }, stem, args)
    logger.success(f"dc_pages: {n} air-permit documents queued")


# --------------------------------------------------------------------------- ARA permits (Socrata + dataviewer)
def soda_all(http, url: str, where: str = "", order: str = "") -> List[Dict]:
    out, offset = [], 0
    while True:
        params = {"$limit": "5000", "$offset": str(offset)}
        if where:
            params["$where"] = where
        if order:
            params["$order"] = order
        r = http.get(url, params=params, timeout=120)
        r.raise_for_status()
        chunk = r.json()
        out.extend(chunk)
        if len(chunk) < 5000:
            return out
        offset += 5000


def dataviewer_files(http, listing_url: str) -> List[Tuple[str, str]]:
    r = http.get(listing_url, headers={"Referer": OPENDATA_REFERER})
    if r.status_code != 200:
        return []
    files = []
    for m in re.finditer(r'title="([^"]+)"[^>]*onclick="[^"]*DownloadFileHandler\.ashx\?ID=(\d+)', r.text):
        files.append((m.group(2), htmlmod.unescape(m.group(1))))
    if not files:
        for m in re.finditer(r"DownloadFileHandler\.ashx\?ID=(\d+)", r.text):
            files.append((m.group(1), f"{m.group(1)}.pdf"))
    return list(dict.fromkeys(files))


def lane_ara(http, index, rows_log, matcher, budget, args, pdf_dir):
    rows = soda_all(http, SODA_ARA)
    full = OUTPUT_DIR / "md_mde_ara_permits_full.csv"
    fields = ["site_no", "alternate_ai_id", "site_name", "permit_type_activity", "permit_category",
              "permit_status", "status", "permit_start_date", "permit_end_date", "application_recieved",
              "county", "addressinfo", "city_state_zip", "document_url", "is_dc", "match_reason"]
    n_dc = 0
    with open(full, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            reason = matcher.match(r.get("site_name", ""), r.get("addressinfo", ""), r.get("city_state_zip", ""))
            r["document_url"] = (r.get("document") or {}).get("url", "")
            r["is_dc"], r["match_reason"] = int(bool(reason)), reason
            w.writerow({k: r.get(k, "") for k in fields})
            n_dc += bool(reason)
    logger.info(f"ara: {len(rows)} permits to operate, {n_dc} data-center matches -> {full.name}")
    targets = [r for r in rows if r["document_url"] and (r["is_dc"] or args.ara_all)]
    for r in targets:
        files = dataviewer_files(http, r["document_url"])
        if not files:
            logger.warning(f"  ara: no files listed for {r.get('site_name')} ({r['document_url']})")
            continue
        for fid, fname in files:
            doc_url = f"{DATAVIEWER}/DownloadFileHandler.ashx?ID={fid}"
            stem = pdf_dir / f"ara_{slug(r.get('alternate_ai_id') or r.get('site_no'), 12)}_{slug(Path(fname).stem, 70)}"
            download_row(http, index, budget, {
                "source": "ara", "section": r.get("permit_category", ""), "snapshot": "live",
                "site_id": r.get("alternate_ai_id") or r.get("site_no", ""), "facility": r.get("site_name", ""),
                "address": f"{r.get('addressinfo', '')} {r.get('city_state_zip', '')}".strip(),
                "project": "", "permit_type": r.get("permit_type_activity", ""),
                "permit_status": f"{r.get('permit_status', '')}/{r.get('status', '')}",
                "permit_date": f"{(r.get('permit_start_date') or '')[:10]}..{(r.get('permit_end_date') or '')[:10]}",
                "doc_label": fname, "doc_url": doc_url, "archive_url": r["document_url"],
                "match_reason": r["match_reason"], "is_dc": r["is_dc"],
            }, stem, args, headers={"Referer": r["document_url"]}, allow_wayback=False)
    logger.success(f"ara: {len(targets)} permit rows processed")


# --------------------------------------------------------------------------- HB1200 public notices (index only)
def lane_notices(http, index, rows_log, matcher, budget, args, pdf_dir):
    rows = soda_all(http, SODA_NOTICE, where="media_type='Air Quality Permits'")
    full = OUTPUT_DIR / "md_mde_hb1200_air_notices.csv"
    fields = ["project_name", "applicant_name", "project_address", "project_county", "type_of_permit",
              "status", "final_determination_date", "comments", "mde_link", "is_dc", "match_reason"]
    n_dc = 0
    with open(full, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            reason = matcher.match(r.get("project_name", ""), r.get("applicant_name", ""),
                                   r.get("project_address", ""), r.get("comments", ""))
            r["mde_link"] = (r.get("mde_links") or {}).get("url", "")
            r["is_dc"], r["match_reason"] = int(bool(reason)), reason
            w.writerow({k: r.get(k, "") for k in fields})
            if reason:
                n_dc += 1
                key = f"hb1200:{r.get('project_name', '')}|{r.get('project_address', '')}"
                if key not in index.seen:
                    index.append({"source": "notices", "section": "HB1200 public notice", "snapshot": "live",
                                  "site_id": "", "facility": r.get("project_name", ""),
                                  "address": r.get("project_address", ""), "project": r.get("comments", ""),
                                  "permit_type": r.get("type_of_permit", ""), "permit_status": r.get("status", ""),
                                  "permit_date": (r.get("final_determination_date") or "")[:10], "doc_label": "",
                                  "doc_url": key, "match_reason": reason, "is_dc": 1, "status": "index_only",
                                  "fetched_at": now_iso()})
    logger.success(f"notices: {len(rows)} HB1200 air-permit notices, {n_dc} data-center matches -> {full.name}")


# --------------------------------------------------------------------------- draft Title V
def lane_t5_draft(http, index, rows_log, matcher, budget, args, pdf_dir):
    r = http.get(T5_DRAFT_URL)
    rows = parse_amp_page(r.text, T5_DRAFT_URL)
    n_dc = process_amp_rows(http, index, rows_log, matcher, budget, args, rows, "t5_draft", "live", pdf_dir)
    logger.success(f"t5_draft: {len(rows)} rows, {n_dc} data-center rows")


LANES = {
    "dc_pages": lane_dc_pages, "amp_live": lane_amp_live, "ara": lane_ara, "cdx_docs": lane_cdx_docs,
    "amp_wayback": lane_amp_wayback, "notices": lane_notices, "t5_draft": lane_t5_draft,
}


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default=",".join(ALL_SOURCES), help="Comma list of lanes to run")
    ap.add_argument("--terms", default="", help="Extra comma-separated operator terms to match")
    ap.add_argument("--gen-min", type=int, default=10, help="Generator count that flags a row as a fleet")
    ap.add_argument("--ara-all", action="store_true", help="Download every ARA permit document, not just data centers")
    ap.add_argument("--dc-page-all", action="store_true", help="Data-center pages: also non-air (brownfield/water) docs")
    ap.add_argument("--wayback-since", default="2018", help="Earliest Wayback snapshot year for the index page")
    ap.add_argument("--wayback-max", type=int, default=0, help="Only the N most recent snapshots (0 = all)")
    ap.add_argument("--limit", type=int, default=0, help="Max downloads this run (0 = no per-run limit)")
    ap.add_argument("--max-downloads", type=int, default=800, help="Cap on total documents in the index")
    ap.add_argument("--no-download", action="store_true", help="Build the index only")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    logger.add(OUTPUT_DIR / "md_mde_dc_permits.log", level="INFO", rotation="5 MB")

    index = Index(OUTPUT_DIR / INDEX_NAME)
    rows_log = RowsLog(OUTPUT_DIR / "md_mde_public_review_rows_full.csv")
    matcher = DCMatcher([t.strip() for t in args.terms.split(",") if t.strip()], gen_min=args.gen_min)
    http = Http(args.delay, args.timeout)

    left = max(0, args.max_downloads - index.n_ok)
    if args.limit:
        left = min(left, args.limit)
    budget = {"left": left}
    logger.info(f"Download budget this run: {left}")

    for lane in [s.strip() for s in args.sources.split(",") if s.strip()]:
        fn = LANES.get(lane)
        if fn is None:
            logger.error(f"Unknown source {lane!r}; choose from {ALL_SOURCES}")
            continue
        logger.info(f"=== lane {lane}")
        try:
            fn(http, index, rows_log, matcher, budget, args, pdf_dir)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"lane {lane} failed: {e}")
    logger.success(f"Index: {index.path} ({index.n_ok} documents on disk, {budget['left']} budget left)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
