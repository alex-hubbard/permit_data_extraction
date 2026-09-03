#!/usr/bin/env python3
"""Download North Carolina DAQ air-permit documents for data centers.

NC DEQ's Division of Air Quality publishes facility records through a
Laserfiche WebLink 11 public portal (https://edocs.deq.nc.gov/AirQuality/,
repo "AirQuality", dbid 0).  The Angular UI talks to unauthenticated JSON
services that this script drives directly (all POST, JSON body, cookies from
one warm-up GET of Browse.aspx):

  FolderListingService.aspx/GetFolderListing2
      {repoName, folderId, getNewListing, start, end, sortColumn, sortAscending}
      Tree: \\Public\\Facilities\\<County (NN)>\\<7-digit facility id>\\<docs>
  SearchService.aspx/GetSearchListing
      {repoName, searchSyn, searchUuid, sortColumn, startIdx, endIdx,
       getNewListing, sortOrder, displayInGridView}
      searchSyn is Laserfiche search syntax on the "Facility" template, e.g.
        {[Facility]:[Facility ID]="1400204"}
        {[Facility]:[NAICS Code]="518210"}
        {[Facility]:[Facility Name]="*Microsoft*"}
      First call: searchUuid=null, getNewListing=true -> hitCount + searchUUID;
      later pages reuse searchUUID with getNewListing=false.
  FolderListingService.aspx/GetMetaData   {repoName, entryId}  (field values)
  ElectronicFile.aspx?docid=<entryId>&dbid=0&repo=AirQuality   -> the PDF

Facility discovery (merged on DAQ facility id):
  1. DAQ Facility Mapping Tool feature layer (ArcGIS, ~2,900 facilities incl.
     permit-exempt/registered ones, with NAICS + SIC labels).
  2. DAQ "All Active Air Permits" spreadsheet (files.nc.gov, ~1,600 rows).
  3. Laserfiche metadata searches: NAICS Code 518210 and Facility Name
     wildcards for every dc_targets entity term (+ NC-specific aliases such
     as "Tapaha" = Google Lenoir, "Andale" = Meta Forest City).
Facilities are tiered: A = data-center industry code (NAICS 518210 / SIC 7374)
or "data center" in the name; B = adjacent industry code (541512, 737x, telecom)
or operator-name match with unknown industry; C = operator-name match with a
clearly unrelated industry code (e.g. an Amazon fulfilment center) — tier C is
indexed but not downloaded unless --include-tier-c.

Documents: by default the Permitting / Public-Participation / Modeling groups
(permits, permit reviews, applications, draft permits, modeling reviews —
the "AQ_F_<facid>_<yyyymmdd>_<GROUP>_<Type>_<rev>" naming); --all-docs adds
compliance, emission-inventory and enforcement records.

Resumable: the index CSV is appended one row per document as it is handled;
on restart, entry ids already present with an existing file are skipped.

Output layout
-------------
  <RAW_DATA_DIR>/nc_daq_dc_permits/
    pdfs/<doc name>.pdf              e.g. AQ_F_1400204_20250512_PRMT_PmtRvw_T10.pdf
    nc_daq_dc_permits_index.csv      one row per document (all tiers)
    nc_daq_dc_facilities.csv         matched facilities with tier / reasons
    nc_daq_gis_facilities.csv        cached GIS facility dump
    all_permitted.xlsx               cached DAQ active-permit list
    nc_daq_dc_permits.log
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

import pandas as pd
import requests
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

import dc_targets  # noqa: E402

BASE = "https://edocs.deq.nc.gov/AirQuality/"
REPO = "AirQuality"
GIS_QUERY = ("https://services2.arcgis.com/kCu40SDxsCGcuUWO/arcgis/rest/services/"
             "ethan_Test1/FeatureServer/0/query")
ALL_PERMITTED_XLSX = "https://files.nc.gov/deq/Air%20Quality/permits/aapa_reports/all_permitted.xlsx"

OUTPUT_DIR = RAW_DATA_DIR / "nc_daq_dc_permits"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

# NC-specific permit entities that do not carry the operator's brand name.
NC_ALIASES = [
    "Tapaha",            # Tapaha Dynamics, LLC = Google, Lenoir (Caldwell)
    "Andale",            # Andale, Inc. = Meta/Facebook, Forest City (Rutherford)
    "Data Center", "Data Centers", "Datacenter", "Colocation", "Enovum",
    "Amazon Data Services", "Kyndryl",
]

DC_NAME_PATTERN = re.compile(r"data ?cent|colocation|hyperscale|server farm", re.IGNORECASE)
STRONG_NAICS = {"518210"}
STRONG_SIC = {"7374"}
ADJACENT_NAICS_RE = re.compile(r"^(5182|5415|5191|5171|5172|5179|5133)")
ADJACENT_SIC_RE = re.compile(r"^(737\d|4813|4899)$")

# Document-group code embedded in DAQ file names: AQ_F_<id>_<date>_<GROUP>_...
DEFAULT_GROUPS = {"PRMT", "PP", "MOD"}
DOC_NAME_RE = re.compile(r"AQ_F_(\d{7})_(\d{8})_([A-Z]+)_(.*)$")

INDEX_FIELDS = [
    "facility_id", "facility_name", "county", "city", "region", "class_status",
    "naics", "sic", "tier", "match_reason", "entry_id", "doc_name", "doc_type",
    "doc_group", "doc_date", "key_event_date", "author", "signatory",
    "extension", "download_url", "pdf_path", "status", "bytes", "fetched_at",
]
FACILITY_FIELDS = [
    "facility_id", "facility_name", "county", "city", "address", "class_status",
    "naics", "sic", "tier", "match_reason", "sources", "n_docs_total",
    "n_docs_selected", "n_downloaded",
]


# --------------------------------------------------------------------------- #
# Portal client
# --------------------------------------------------------------------------- #
class WebLink:
    def __init__(self, delay: float, timeout: int):
        self.delay = delay
        self.timeout = timeout
        self.sess = requests.Session()
        self.sess.headers.update(HEADERS)
        self._warm()

    def _warm(self) -> None:
        # The JSON services need the WebLinkSession/AcceptsCookies cookies
        # that Browse.aspx sets.
        r = self.sess.get(BASE + "Browse.aspx", params={"id": 10, "dbid": 0, "repo": REPO},
                          timeout=self.timeout)
        r.raise_for_status()
        if "WebLinkSession" not in self.sess.cookies:
            raise RuntimeError("portal did not issue a WebLinkSession cookie")

    def post(self, endpoint: str, body: dict, retries: int = 3) -> dict:
        last: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                r = self.sess.post(BASE + endpoint, data=json.dumps(body),
                                   headers={"Content-Type": "application/json"},
                                   timeout=self.timeout)
                if r.status_code >= 500 and "search handle" in r.text.lower():
                    raise RuntimeError("search handle expired")
                r.raise_for_status()
                payload = r.json()
                data = payload.get("data")
                if data is None:
                    raise RuntimeError(f"no data in response: {r.text[:200]}")
                if isinstance(data, dict) and data.get("failed"):
                    raise RuntimeError(data.get("errMsg") or "service reported failure")
                time.sleep(self.delay)
                return data
            except Exception as e:  # noqa: BLE001
                last = e
                logger.warning(f"{endpoint} attempt {attempt}/{retries}: {e}")
                time.sleep(self.delay * 3 * attempt)
                try:
                    self._warm()
                except Exception as we:  # noqa: BLE001
                    logger.warning(f"re-warm failed: {we}")
        raise RuntimeError(f"{endpoint} failed after {retries} attempts: {last}")

    # ---- listing helpers -------------------------------------------------- #
    def folder(self, folder_id: int, page: int = 500) -> List[dict]:
        out, start = [], 0
        while True:
            d = self.post("FolderListingService.aspx/GetFolderListing2", {
                "repoName": REPO, "folderId": folder_id, "getNewListing": start == 0,
                "start": start, "end": start + page, "sortColumn": "", "sortAscending": True,
            })
            cols = [c["name"] for c in d.get("colTypes", [])]
            for r in d.get("results", []):
                row = dict(zip(cols, r.get("data", [])))
                row["_entry_id"], row["_type"], row["_name"] = r["entryId"], r["type"], r["name"]
                out.append(row)
            total = d.get("totalEntries", 0)
            start += page
            if start >= total or not d.get("results"):
                break
        return out

    def search(self, syn: str, page: int = 100, max_rows: int = 5000) -> List[dict]:
        """Run a Laserfiche search; returns result rows keyed by column name."""
        out, start, uuid = [], 0, None
        while True:
            d = self.post("SearchService.aspx/GetSearchListing", {
                "repoName": REPO, "searchSyn": syn, "searchUuid": uuid, "sortColumn": "",
                "startIdx": start, "endIdx": start + page, "getNewListing": uuid is None,
                "sortOrder": 2, "displayInGridView": False,
            })
            uuid = d.get("searchUUID") or uuid
            cols = [c["name"] for c in d.get("columns", [])]
            rows = d.get("results", [])
            for r in rows:
                row = dict(zip(cols, r.get("data", [])))
                row["_entry_id"], row["_type"], row["_name"] = r["entryId"], r["type"], r["name"]
                row["_link_to"] = r.get("shortcutTargetId") or 0
                out.append(row)
            total = d.get("hitCount", 0)
            start += page
            if not rows or start >= total or len(out) >= max_rows:
                break
        return out

    def download(self, entry_id: int, dest: Path) -> int:
        url = BASE + f"ElectronicFile.aspx?docid={entry_id}&dbid=0&repo={REPO}"
        r = self.sess.get(url, timeout=self.timeout * 3)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        if not (r.content.startswith(b"%PDF") or "pdf" in ctype or "octet-stream" in ctype
                or "msword" in ctype or "officedocument" in ctype):
            raise RuntimeError(f"unexpected content-type {ctype!r} ({len(r.content)} bytes)")
        dest.write_bytes(r.content)
        time.sleep(self.delay)
        return len(r.content)


# --------------------------------------------------------------------------- #
# Facility discovery
# --------------------------------------------------------------------------- #
def _code(label) -> str:
    """'Data Processing, Hosting, and Related Services (518210)' -> '518210'."""
    if label is None or (isinstance(label, float) and pd.isna(label)):
        return ""
    s = str(label).strip()
    m = re.search(r"\((\d{3,6})\)\s*$", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"\d{3,6}", s) else ""


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 &-]", " ", str(text).lower())
    return re.sub(r"\s+", " ", text).strip()


def _term_matches(name: str, terms: Iterable[str]) -> List[str]:
    n = _norm(name)
    hits = []
    for t in terms:
        tn = _norm(t)
        if not tn:
            continue
        if len(tn) < 6:
            if re.search(rf"\b{re.escape(tn)}\b", n):
                hits.append(t)
        elif tn in n:
            hits.append(t)
    return hits


def tier_for(name: str, naics: str, sic: str, term_hits: List[str], extra_reasons: List[str]) -> (str, str):
    reasons = list(extra_reasons)
    strong = naics in STRONG_NAICS or sic in STRONG_SIC
    adjacent = bool(ADJACENT_NAICS_RE.match(naics or "") or ADJACENT_SIC_RE.match(sic or ""))
    if strong:
        reasons.append(f"naics/sic {naics or sic}")
    if DC_NAME_PATTERN.search(name or ""):
        reasons.append("dc name")
    if term_hits:
        reasons.append("operator:" + "|".join(term_hits[:3]))
    if strong or DC_NAME_PATTERN.search(name or ""):
        return "A", "; ".join(reasons)
    if adjacent:
        reasons.append(f"adjacent naics/sic {naics or sic}")
        return "B", "; ".join(reasons)
    if term_hits:
        if naics or sic:
            return "C", "; ".join(reasons)
        return "B", "; ".join(reasons)
    return "", "; ".join(reasons)


def load_gis(cache: Path, timeout: int) -> pd.DataFrame:
    if cache.exists():
        return pd.read_csv(cache, dtype=str).fillna("")
    rows, off = [], 0
    while True:
        r = requests.get(GIS_QUERY, params={"where": "1=1", "outFields": "*", "resultOffset": off,
                                            "resultRecordCount": 2000, "f": "json"},
                         headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        feats = j.get("features", [])
        rows += [f["attributes"] for f in feats]
        if not j.get("exceededTransferLimit") or not feats:
            break
        off += len(feats)
    df = pd.DataFrame(rows).astype(str).replace({"None": "", "nan": ""})
    df.to_csv(cache, index=False)
    logger.info(f"GIS facility layer: {len(df)} facilities cached to {cache}")
    return df


def load_all_permitted(cache: Path, timeout: int) -> pd.DataFrame:
    if not cache.exists():
        r = requests.get(ALL_PERMITTED_XLSX, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        cache.write_bytes(r.content)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_excel(cache, header=1, dtype=str)
    df.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]
    df = df.fillna("")
    df["Facility ID"] = df["Facility ID"].str.replace(r"\.0$", "", regex=True).str.zfill(7)
    return df


def discover_facilities(wl: Optional[WebLink], terms: List[str], out_dir: Path, timeout: int,
                        portal_name_search: bool, explicit_ids: List[str]) -> Dict[str, dict]:
    fac: Dict[str, dict] = {}

    def upsert(fid: str, **kw):
        fid = str(fid).strip().zfill(7)
        if not re.fullmatch(r"\d{7}", fid):
            return None
        f = fac.setdefault(fid, {"facility_id": fid, "facility_name": "", "county": "", "city": "",
                                 "address": "", "class_status": "", "naics": "", "sic": "",
                                 "sources": set(), "term_hits": [], "extra": []})
        for k, v in kw.items():
            if k in ("sources",):
                f["sources"].update(v)
            elif k == "term_hits":
                f["term_hits"] = sorted(set(f["term_hits"]) | set(v))
            elif k == "extra":
                f["extra"] = sorted(set(f["extra"]) | set(v))
            elif v and not f.get(k):
                f[k] = v
        return f

    # 1. GIS layer ---------------------------------------------------------- #
    gis = load_gis(out_dir / "nc_daq_gis_facilities.csv", timeout)
    for _, r in gis.iterrows():
        name, naics, sic = r["FACILITY_NAME"], _code(r["NAICS"]), _code(r["SIC"])
        hits = _term_matches(name, terms)
        if not (hits or naics in STRONG_NAICS or sic in STRONG_SIC or DC_NAME_PATTERN.search(name)
                or ADJACENT_NAICS_RE.match(naics) or ADJACENT_SIC_RE.match(sic)):
            continue
        upsert(r["DAQ_FACILITY_ID"], facility_name=name, county=r["COUNTY"], city=r["CITY"],
               address=r["LOCATION_ADDRESS_LINE_1"], class_status=r["CLASS_STATUS"],
               naics=naics, sic=sic, sources={"gis"}, term_hits=hits)
    logger.info(f"GIS layer: {len(fac)} candidate facilities")

    # 2. Active-permit spreadsheet ------------------------------------------ #
    try:
        ap = load_all_permitted(out_dir / "all_permitted.xlsx", timeout)
        n0 = len(fac)
        for _, r in ap.iterrows():
            name, sic = r["Facility Name"], _code(r["SIC"])
            hits = _term_matches(name, terms)
            if not (hits or sic in STRONG_SIC or DC_NAME_PATTERN.search(name) or ADJACENT_SIC_RE.match(sic)):
                continue
            upsert(r["Facility ID"], facility_name=name, county=r["County"], city=r["Facility City"],
                   address=r["Facility Address Line 1"], class_status=r["Permit Class"], sic=sic,
                   sources={"all_permitted"}, term_hits=hits)
        logger.info(f"all_permitted.xlsx: {len(fac) - n0} new candidates ({len(ap)} active permits)")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"all_permitted.xlsx unavailable: {e}")

    # 3. Laserfiche metadata searches --------------------------------------- #
    if wl is not None:
        n0 = len(fac)
        try:
            rows = wl.search('{[Facility]:[NAICS Code]="518210"}', max_rows=3000)
            for row in rows:
                upsert(row.get("f_Facility ID", ""), facility_name=row.get("f_Facility Name", ""),
                       county=row.get("f_County", ""), city=row.get("f_City/Town", ""),
                       naics="518210", sources={"lf_naics"})
            logger.info(f"Laserfiche NAICS 518210 search: {len(rows)} docs, {len(fac) - n0} new facilities")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"NAICS search failed: {e}")
        if portal_name_search:
            n1 = len(fac)
            for i, t in enumerate(terms, 1):
                syn = '{[Facility]:[Facility Name]="*%s*"}' % t.replace('"', "")
                try:
                    rows = wl.search(syn, page=100, max_rows=600)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[{i}/{len(terms)}] name search {t!r} failed: {e}")
                    continue
                ids = {}
                for row in rows:
                    fid = row.get("f_Facility ID", "")
                    if fid:
                        ids.setdefault(fid, row)
                for fid, row in ids.items():
                    upsert(fid, facility_name=row.get("f_Facility Name", ""), county=row.get("f_County", ""),
                           city=row.get("f_City/Town", ""), sources={"lf_name"}, term_hits=[t])
                if rows:
                    logger.info(f"[{i}/{len(terms)}] {t!r}: {len(rows)} docs / {len(ids)} facilities")
            logger.info(f"Laserfiche name searches: {len(fac) - n1} new facilities")

    for fid in explicit_ids:
        f = upsert(fid, sources={"explicit"})
        if f is not None:
            f["extra"].append("explicit")

    # Tiering ---------------------------------------------------------------- #
    for f in fac.values():
        tier, reason = tier_for(f["facility_name"], f["naics"], f["sic"], f["term_hits"], f["extra"])
        if "explicit" in f["extra"]:
            tier = tier or "A"
        f["tier"], f["match_reason"] = tier, reason
        f["sources"] = "|".join(sorted(f["sources"]))
    return {k: v for k, v in fac.items() if v["tier"]}


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
def doc_group(name: str) -> str:
    m = DOC_NAME_RE.search(name or "")
    return m.group(3) if m else ""


def is_document(row: dict) -> bool:
    et = str(row.get("EntryType", ""))
    return et in ("-2", "Electronic Document", "Document") or bool(row.get("Extension")) \
        or row.get("_type") == -2


def safe_stem(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._()-]+", "_", name).strip("_")[:150]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--facility-ids", default="",
                    help="Comma-separated 7-digit DAQ facility ids to fetch (added to discovery)")
    ap.add_argument("--only-ids", action="store_true",
                    help="Skip discovery entirely; use --facility-ids only")
    ap.add_argument("--terms", default="", help="Comma-separated operator terms (default: dc_targets NC + NC aliases)")
    ap.add_argument("--no-portal-name-search", action="store_true",
                    help="Skip the slow Laserfiche Facility-Name wildcard searches (~10-30 s each)")
    ap.add_argument("--all-docs", action="store_true",
                    help="Download every document group (default: PRMT, PP, MOD only)")
    ap.add_argument("--groups", default=",".join(sorted(DEFAULT_GROUPS)),
                    help="Document-group codes to download (from AQ_F_<id>_<date>_<GROUP>_ names)")
    ap.add_argument("--include-tier-c", action="store_true",
                    help="Also download tier-C facilities (operator-name match, unrelated industry code)")
    ap.add_argument("--no-download", action="store_true", help="Index only")
    ap.add_argument("--limit", type=int, default=0, help="Stop after this many downloads (tests)")
    ap.add_argument("--max-pdfs", type=int, default=1000, help="Hard cap on downloads per run")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    logger.add(OUTPUT_DIR / "nc_daq_dc_permits.log", rotation="20 MB", level="INFO")

    index_path = OUTPUT_DIR / "nc_daq_dc_permits_index.csv"
    done: Dict[str, str] = {}
    if index_path.exists():
        with open(index_path, newline="", encoding="utf-8") as f:
            for old in csv.DictReader(f):
                if old.get("status") == "ok" and old.get("pdf_path") and Path(old["pdf_path"]).exists():
                    done[old["entry_id"]] = old["pdf_path"]
                elif old.get("status") == "indexed":
                    done[old["entry_id"]] = ""
        logger.info(f"Resuming: {sum(1 for v in done.values() if v)} downloaded, "
                    f"{sum(1 for v in done.values() if not v)} index-only rows already present")

    explicit_ids = [x.strip() for x in args.facility_ids.split(",") if x.strip()]
    if args.terms:
        terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    else:
        terms = dc_targets.entity_terms("NC") + NC_ALIASES
        seen, uniq = set(), []
        for t in terms:
            if t.lower() not in seen:
                seen.add(t.lower())
                uniq.append(t)
        terms = uniq

    wl = WebLink(args.delay, args.timeout)

    if args.only_ids:
        facilities = discover_facilities(None, [], OUTPUT_DIR, args.timeout, False, explicit_ids)
        facilities = {k: v for k, v in facilities.items() if k in {x.zfill(7) for x in explicit_ids}}
    else:
        facilities = discover_facilities(wl, terms, OUTPUT_DIR, args.timeout,
                                         not args.no_portal_name_search, explicit_ids)
    tiers = {t: sum(1 for f in facilities.values() if f["tier"] == t) for t in "ABC"}
    logger.success(f"{len(facilities)} candidate facilities (tier A {tiers['A']}, B {tiers['B']}, C {tiers['C']})")
    for f in sorted(facilities.values(), key=lambda x: (x["tier"], x["facility_id"])):
        logger.info(f"  tier {f['tier']} {f['facility_id']} {f['facility_name'][:55]!r} "
                    f"[{f['county']}; {f['class_status']}; naics {f['naics'] or '-'}] {f['match_reason']}")

    wanted_groups = None if args.all_docs else {g.strip().upper() for g in args.groups.split(",") if g.strip()}
    download_tiers = {"A", "B"} | ({"C"} if args.include_tier_c else set())

    new_file = not index_path.exists()
    n_dl = n_fail = n_skip = 0
    stop = False
    with open(index_path, "a", newline="", encoding="utf-8") as idx_f:
        writer = csv.DictWriter(idx_f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()

        order = sorted(facilities.values(), key=lambda f: (f["tier"], f["facility_id"]))
        for fi, fac in enumerate(order, 1):
            fid = fac["facility_id"]
            try:
                rows = wl.search('{[Facility]:[Facility ID]="%s"}' % fid, page=100, max_rows=5000)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{fi}/{len(order)}] {fid} {fac['facility_name']}: listing failed: {e}")
                fac.update(n_docs_total=-1, n_docs_selected=0, n_downloaded=0)
                continue
            docs, seen_names = [], set()
            for r in rows:
                if not is_document(r):
                    continue
                key = (r.get("_name") or "").strip().lower()
                if key in seen_names:     # copies/shortcuts in curated folders
                    continue
                seen_names.add(key)
                docs.append(r)
            if not fac["facility_name"] and docs:
                fac["facility_name"] = docs[0].get("f_Facility Name", "")
            selected = [d for d in docs if wanted_groups is None or doc_group(d["_name"]) in wanted_groups]
            fac.update(n_docs_total=len(docs), n_docs_selected=len(selected), n_downloaded=0)
            logger.info(f"[{fi}/{len(order)}] tier {fac['tier']} {fid} {fac['facility_name'][:60]!r}: "
                        f"{len(docs)} docs, {len(selected)} selected")

            for d in selected:
                eid = str(d["_entry_id"])
                if eid in done and (done[eid] or args.no_download):
                    n_skip += 1
                    if done[eid]:
                        fac["n_downloaded"] += 1
                    continue
                row = {
                    "facility_id": fid, "facility_name": fac["facility_name"], "county": fac["county"]
                    or d.get("f_County", ""), "city": fac["city"] or d.get("f_City/Town", ""),
                    "region": d.get("f_Region", ""), "class_status": fac["class_status"],
                    "naics": fac["naics"], "sic": fac["sic"], "tier": fac["tier"],
                    "match_reason": fac["match_reason"], "entry_id": eid, "doc_name": d["_name"],
                    "doc_type": d.get("f_Document Type", ""), "doc_group": doc_group(d["_name"]),
                    "doc_date": d.get("f_Document Date", ""), "key_event_date": d.get("f_Key Event Date", ""),
                    "author": d.get("f_Author", ""), "signatory": d.get("f_Signatory", ""),
                    "extension": d.get("Extension", ""),
                    "download_url": BASE + f"ElectronicFile.aspx?docid={eid}&dbid=0&repo={REPO}",
                    "pdf_path": "", "status": "indexed", "bytes": "", "fetched_at": "",
                }
                do_get = (not args.no_download and fac["tier"] in download_tiers and not stop)
                if do_get:
                    ext = (d.get("Extension") or "pdf").lower().lstrip(".")
                    dest = pdf_dir / f"{safe_stem(d['_name'])}.{ext}"
                    if dest.exists() and eid not in done:
                        dest = pdf_dir / f"{safe_stem(d['_name'])}_e{eid}.{ext}"
                    try:
                        target = int(d.get("_link_to") or 0) or int(eid)
                        size = wl.download(target, dest)
                        row.update(pdf_path=str(dest), status="ok", bytes=size,
                                   fetched_at=datetime.now().isoformat(timespec="seconds"))
                        n_dl += 1
                        fac["n_downloaded"] += 1
                        done[eid] = str(dest)
                    except Exception as e:  # noqa: BLE001
                        n_fail += 1
                        row.update(status=f"error: {e}"[:200])
                        logger.warning(f"  {d['_name']}: {e}")
                    if (args.limit and n_dl >= args.limit) or n_dl >= args.max_pdfs:
                        stop = True
                elif eid in done:
                    continue
                writer.writerow(row)
                idx_f.flush()
                if eid not in done:
                    done[eid] = row["pdf_path"]
            if stop:
                logger.warning(f"Download cap reached ({n_dl}); remaining facilities are indexed only")

    with open(OUTPUT_DIR / "nc_daq_dc_facilities.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FACILITY_FIELDS, extrasaction="ignore")
        w.writeheader()
        for fac in sorted(facilities.values(), key=lambda x: (x["tier"], x["facility_id"])):
            w.writerow(fac)
    logger.success(f"Downloaded {n_dl} files ({n_fail} failed, {n_skip} already present) -> {pdf_dir}")
    logger.success(f"Index: {index_path}; facilities: {OUTPUT_DIR / 'nc_daq_dc_facilities.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
