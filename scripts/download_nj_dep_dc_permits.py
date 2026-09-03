#!/usr/bin/env python3
"""Download New Jersey DEP air-permit documents for data centers — or, while the
NJDEP portals stay behind their bot wall, build the best possible OPRA /
DocMiner target list (``--targets-only``).

Access situation (re-tested 2026-09-02 from the LBL network)
-----------------------------------------------------------
* NJDEP DataMiner moved from www13.state.nj.us (host no longer resolves) to
  https://njems.nj.gov/DataMiner .  Every request — plain ``requests`` with
  browser headers, a real headless Chromium, and a third-party fetch service —
  gets an Imperva/Incapsula "Request unsuccessful" incident page.  Same for the
  new OPRA-free document library **NJDEP DocMiner** (https://docminer.nj.gov,
  launched Oct 2025, search by site / Program Interest ID) and for every HTML
  page under https://dep.nj.gov/ (BOSS air permitting, public notices,
  wp-json).  We do not try to defeat the challenge.
* What *is* served without a challenge: static uploads on dep.nj.gov, in
  particular the Bureau of Stationary Sources public-notice repository
  ``/wp-content/uploads/boss/public-notices/<PI>-<activity>-draftpermit.pdf``
  (Title V "BOP" drafts and publicly noticed "PCP" preconstruction drafts) and
  the biweekly DEP Bulletin PDFs (``/wp-content/uploads/bulletin/buYYYY_MMDD.pdf``
  — land-use / NJPDES / solid-waste actions only; it carries no air-permit
  notices, so it is not used here).
* EPA ECHO's ICIS-Air REST service returns all ~7,200 NJ air facilities
  (majors, synthetic minors and minors) and the NJ SourceID embeds the NJEMS
  Program Interest (PI) number: ``NJ0000003402318940`` = FIPS 34 + county 023 +
  PI 18940; ``NJ00012845`` = PI 12845.  The local FRS extracts in
  data/external add NJPDES / RCRA / EIS records with addresses.

Lanes
-----
1. **targets** (always): LBL data-center map rows for NJ + dc_targets aliases +
   a curated NJ list (DataOne/Nebius Vineland, CoreWeave Kenilworth NEST, …)
   are matched to ECHO/FRS facilities by normalized street address (with a
   city or <5 km geo check), by operator / "data center" name patterns and by
   NAICS.  Output ``nj_dc_permit_targets.csv`` (one row per matched agency
   record, plus rows for targets with no agency record).
2. **portal probe** (skipped with --targets-only / --skip-portal-probe): one
   polite GET (+1 retry) each of DataMiner, DocMiner and the BOSS public-notice
   page; results logged to ``portal_access_probe.csv``.  If the BOSS page ever
   answers, every PDF link on it is indexed and those whose PI prefix or link
   text matches a target are downloaded.
3. **public-notice probe**: for target PIs that plausibly hold Title V / noticed
   permits (majors, 80 % synthetic minors, curated PIs — or all PIs with
   --probe-all-pis) HEAD-probe the draft-permit URL pattern for the requested
   years and download hits.

Output layout
-------------
  <RAW_DATA_DIR>/nj_dep_dc_permits/
    pdfs/nj_<PI>_<activity>_<kind>.pdf
    nj_dep_dc_permits_index.csv       one row per document URL tried (appended as we go)
    nj_dc_permit_targets.csv          OPRA / DocMiner target list
    nj_echo_air_facilities.csv        cached ECHO ICIS-Air NJ facility list
    frs_nj_program_links.csv          cached FRS NJ program-link subset
    portal_access_probe.csv           access-probe log
    nj_dep_dc_permits.log

Resumable: index rows with status "ok" whose file exists are skipped;
"missing" (404) probes are not repeated unless --reprobe-missing.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd
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

OUTPUT_DIR = RAW_DATA_DIR / "nj_dep_dc_permits"
FRS_LINKS = REPO_ROOT / "data" / "external" / "FRS_PROGRAM_LINKS.csv"
FRS_FACS = REPO_ROOT / "data" / "external" / "FRS_FACILITIES.csv"

ECHO_FAC = "https://echodata.epa.gov/echo/air_rest_services.get_facilities"
ECHO_QID = "https://echodata.epa.gov/echo/air_rest_services.get_qid"
DATAMINER_URL = "https://njems.nj.gov/DataMiner"
DATAMINER_AIR_URL = ("https://njems.nj.gov/DataMiner/Search/SearchByCategory"
                     "?isExternal=y&getCategory=y&catName=Air+Quality+Permitting+and+Reporting")
DOCMINER_URL = "https://docminer.nj.gov/DocMiner/"
BOSS_NOTICES_URL = "https://dep.nj.gov/boss/public-notices/"
NOTICE_PDF_BASE = "https://dep.nj.gov/wp-content/uploads/boss/public-notices/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

INDEX_FIELDS = [
    "lane", "target_ids", "njems_pi_id", "facility_name", "activity_number",
    "doc_kind", "url", "http_status", "status", "pdf_path", "bytes", "fetched_at",
]
TARGET_FIELDS = [
    "target_id", "dc_operator", "dc_facility_name", "dc_address", "dc_city",
    "dc_source", "lbl_status", "lbl_it_load_mw", "dc_lat", "dc_lon",
    "match_method", "match_confidence", "agency_system", "agency_id",
    "njems_pi_id", "agency_name", "agency_street", "agency_city", "agency_county",
    "agency_naics", "air_classification", "air_status", "air_programs",
    "frs_registry_id", "notice_docs_found", "notes",
]
PROBE_FIELDS = ["probed_at", "url", "http_status", "result", "bytes", "detail"]

# ----------------------------------------------------------------------------
# Curated New Jersey target list (things the LBL map lacks or mislabels).
# ----------------------------------------------------------------------------
# Each entry: operator, facility, address, city, extra PI ids to attach, notes.
CURATED_TARGETS = [
    dict(operator="DataOne / Nebius Group (Microsoft-contracted)", facility="DataOne Vineland (Nebius New Jersey, 300 MW)",
         address="3963 S Lincoln Avenue", city="Vineland", pis=["75894"],
         notes="ECHO 'DATAONE' PI 75894 (Minor Emissions). NJDEP (Aug 2026): no permits issued and no application under review "
               "for the ~62 on-site gas gensets; application for 30+ Bergen gas engines withdrawn May 2026; 300 MW Bloom fuel "
               "cells planned. FRS NJPDES NJG367028 'LINCOLN DATA CENTER'. Request all air permit / application / enforcement records."),
    dict(operator="CoreWeave, Inc.", facility="CoreWeave NEST 1 (former Merck Kenilworth campus, 250 MW, on-site power)",
         address="2000 Galloping Hill Road", city="Kenilworth", pis=["41806", "41741"],
         notes="Site = Merck Sharp & Dohme Kenilworth (PI 41806, Title V major) + EF Kenilworth Cogeneration (PI 41741, Title V major, "
               "Appendix A #53). CoreWeave conversion under construction since Sep 2025, operational early 2027 with on-site generation; "
               "request current Title V permits and any modification / new PI filed by CoreWeave or NEST."),
    dict(operator="Digital Realty Trust, Inc.", facility="New York EWR10 (300 JFK Blvd E / 300 Boulevard East)",
         address="300 Boulevard East", city="Weehawken", pis=["12320"],
         notes="ECHO lists the building as LEVEL 3 COMMUNICATIONS INC, 300 BOULEVARD E (PI 12320)."),
    dict(operator="Digital Realty Trust, Inc. (Bloomberg tenant)", facility="1115 Centennial Avenue",
         address="1115 Centennial Avenue", city="Piscataway", pis=["19375"],
         notes="LBL map has the address as 1116 Centennial; ECHO 'BLOOMBERG PISCATAWAY DATA CTR' PI 19375."),
    dict(operator="Equinix, Inc.", facility="NY2 / NY3 LLC (275 Hartz Way)",
         address="275 Hartz Way", city="Secaucus", pis=["12349"],
         notes="ECHO 'NY3 LLC' 275 HARTZ WAY PI 12349; LBL map lists NY2 at 225 Hartz Way."),
    dict(operator="Princeton Digital Group (PDG)", facility="'Princeton Digital NJ1' (listing on datacenters.com)",
         address="", city="", pis=[],
         notes="RESOLVED: PDG's NJ1 is its Nanjing (Jiangsu, China) campus; PDG operates only in Asia. No New Jersey facility exists; "
               "no ECHO/FRS record. Excluded from the OPRA appendix."),
    dict(operator="Microsoft / Amazon / Google / Meta / Oracle", facility="(hyperscaler self-build search)",
         address="", city="", pis=[],
         notes="No NJ ICIS-Air facility under these names; the only Amazon rows are fulfillment centers (NAICS 493110). Microsoft's NJ "
               "capacity is contracted from Nebius/DataOne Vineland (above)."),
]

# Operator / name signals that mark an agency record as a data center.
DC_NAME_RE = re.compile(
    r"data ?(?:cent|ctr)|datacenter|colocation|computer ctr|server farm|"
    r"equinix|digital realty|digital piscataway|digital 650|digital totowa|telx|cyxtera|evoque|"
    r"\bqts\b|cervalis|cyrusone|cottontail|datapipe|rackspace|coresite|databank|cologix|"
    r"iron mountain|io new jersey|io data|dataone|nebius|coreweave|vantage data|stack infra|"
    r"aligned data|edgeconnex|netrality|databridge|h5 data|365 data|centersquare|evocative|"
    r"sungard availability|11:11|level 3 comm|zayo|abovenet|sidera|neon /|xo communications|"
    r"mci worldcom|verizon business|ny3 llc",
    re.IGNORECASE)
DC_NAICS_PREFIXES = ("518210", "541513", "518111", "517")
WAREHOUSE_NAICS_PREFIXES = ("493",)
# dc_targets terms too generic for NJ agency names.
NJ_TERM_STOPLIST = {"switch", "sprint", "at&t inc", "t mobile", "crown castle inc", "globenet",
                    "interserver", "fcm360", "at&t communications", "hammer communications llc",
                    "aegon services dublin limited", "columbia property trust inc", "lam cloud management llc",
                    "fairfax financial holdings limited", "blue hill data services", "menlo equities vi lp",
                    "dataone"}

SUFFIX = {"street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave", "road": "rd", "rd": "rd",
          "boulevard": "blvd", "blvd": "blvd", "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln",
          "place": "pl", "pl": "pl", "parkway": "pkwy", "pkwy": "pkwy", "court": "ct", "ct": "ct",
          "way": "way", "highway": "hwy", "hwy": "hwy", "route": "rt", "rte": "rt", "rt": "rt",
          "plaza": "plz", "plz": "plz", "circle": "cir", "cir": "cir", "terrace": "ter", "ter": "ter",
          "turnpike": "tpke", "tpke": "tpke", "square": "sq", "sq": "sq"}
DIRS = {"north": "n", "south": "s", "east": "e", "west": "w", "n": "n", "s": "s", "e": "e", "w": "w"}
STOP_TAIL = {"suite", "ste", "floor", "fl", "bldg", "building", "unit", "annex", "rm", "room", "#"}
CITY_ALIASES = {  # LBL city -> agency city/municipality tokens that mean the same place
    "somerset": {"somerset", "franklin"}, "cedar knolls": {"cedar", "hanover"},
    "parsippany": {"parsippany", "parsippany-troy"}, "piscataway": {"piscataway", "edison"},
    "franklin township": {"franklin", "somerset"}, "weehawken": {"weehawken", "jersey"},
    "jersey city": {"jersey", "weehawken"}, "wall township": {"wall"}, "bridgewater township": {"bridgewater"},
    "new jersey": set(),
}
GENERIC_AGENCY_CITY = {"", "new jersey", "undetermined"}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def make_session(timeout_retries: int = 2) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                      "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8"})
    retry = Retry(total=timeout_retries, backoff_factor=1.5, status_forcelist=(502, 503, 504),
                  allowed_methods=("GET", "HEAD"))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def classify_nj_response(r: requests.Response) -> str:
    """ok | js-stub | incident | http-<code> for Imperva-fronted NJ hosts."""
    ct = r.headers.get("Content-Type", "")
    body = r.text[:4000] if "html" in ct else ""
    if "Request unsuccessful" in body or "incident_id=" in body:
        return "incident"
    if len(r.content) < 600 and "_Incapsula_Resource" in body:
        return "js-stub"
    if r.status_code >= 400:
        return f"http-{r.status_code}"
    return "ok"


def pi_from_source_id(sid: str) -> str:
    """NJEMS Program Interest number embedded in an ICIS-Air / FRS AIR id."""
    sid = (sid or "").strip().upper()
    m = re.match(r"^(?:NJ|02)0*34(\d{3})(\d{5})$", sid)
    if m:
        return str(int(m.group(2)))
    m = re.match(r"^NJ0*(\d{1,6})$", sid)
    if m:
        return str(int(m.group(1)))
    return ""


def norm_tokens(addr: str) -> List[str]:
    a = re.sub(r"[^a-z0-9 ]", " ", str(addr or "").lower())
    out = []
    for t in a.split():
        if t in STOP_TAIL:
            break
        out.append(SUFFIX.get(t, DIRS.get(t, t)))
    return out


def addr_keys(addr: str) -> Tuple[Optional[str], Optional[str]]:
    """(strict key, loose key): '755 Secaucus Road' -> ('755secaucusrd', '755secaucus')."""
    t = norm_tokens(addr)
    if not t or not re.match(r"^\d+[a-z]?$", t[0]):
        return None, None
    num = re.sub(r"[a-z]$", "", t[0])
    rest = [x for x in t[1:] if x not in DIRS.values()]
    if not rest:
        return None, None
    name = []
    for i, x in enumerate(rest):
        name.append(x)
        if x in SUFFIX.values() and not (x in ("rt", "hwy", "us") and i == 0):
            break
        if len(name) >= 3:
            break
    strict = num + "".join(name)
    loose = num + rest[0]
    return strict, loose


def city_tokens(city: str) -> Set[str]:
    c = re.sub(r"[^a-z\- ]", " ", str(city or "").lower())
    c = re.sub(r"\b(twp|township|boro|borough|city|town|village|of|the)\b", " ", c)
    return {t for t in re.split(r"[\s,]+", c) if t}


def cities_agree(lbl_city: str, agency_city: str) -> bool:
    ac = str(agency_city or "").strip().lower()
    if ac in GENERIC_AGENCY_CITY:
        return True
    lt = city_tokens(lbl_city) | CITY_ALIASES.get(str(lbl_city or "").strip().lower(), set())
    return bool(lt & city_tokens(ac))


def haversine_km(lat1, lon1, lat2, lon2) -> Optional[float]:
    try:
        lat1, lon1, lat2, lon2 = map(float, (lat1, lon1, lat2, lon2))
    except (TypeError, ValueError):
        return None
    if any(math.isnan(v) for v in (lat1, lon1, lat2, lon2)):
        return None
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


class IndexWriter:
    """CSV appended row by row; loads existing rows for resume."""

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

    def append(self, row: Dict[str, object]) -> None:
        clean = {k: ("" if row.get(k) is None else str(row.get(k))) for k in self.fields}
        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.fields).writerow(clean)
        self.rows.append(clean)


# ----------------------------------------------------------------------------
# Agency facility lists: ECHO ICIS-Air (live, cached) and FRS (local, cached)
# ----------------------------------------------------------------------------
ECHO_KEEP = ["SourceID", "RegistryID", "AIRName", "AIRStreet", "AIRCity", "AIRState", "AIRZip", "AIRCounty",
             "FacFIPSCode", "AIRNAICS", "FacSICCodes", "AIRStatus", "AIRClassification", "AIRUniverse",
             "AIRPrograms", "AIRIDs", "FacLat", "FacLong", "AIRLastEvalDate", "AIRComplStatus"]


def load_echo_nj(out_dir: Path, session: requests.Session, refresh: bool, timeout: int) -> pd.DataFrame:
    cache = out_dir / "nj_echo_air_facilities.csv"
    if cache.exists() and not refresh:
        df = pd.read_csv(cache, dtype=str).fillna("")
        logger.info(f"ECHO NJ facility cache: {len(df)} rows ({cache.name})")
        return df
    r = session.get(ECHO_FAC, params={"output": "JSON", "p_st": "NJ"}, timeout=timeout)
    r.raise_for_status()
    res = r.json()["Results"]
    qid, n = res["QueryID"], int(res.get("QueryRows") or 0)
    logger.info(f"ECHO: {n} NJ air facilities (qid {qid})")
    rows: List[Dict] = []
    page = 1
    while len(rows) < n:
        r2 = session.get(ECHO_QID, params={"output": "JSON", "qid": qid, "pageno": page, "responseset": 5000},
                         timeout=timeout * 3)
        r2.raise_for_status()
        facs = r2.json()["Results"].get("Facilities", [])
        if not facs:
            break
        rows.extend(facs)
        page += 1
        time.sleep(1.0)
    df = pd.DataFrame(rows)
    for c in ECHO_KEEP:
        if c not in df.columns:
            df[c] = ""
    df = df[ECHO_KEEP].fillna("").astype(str)
    df.to_csv(cache, index=False)
    logger.success(f"ECHO NJ facility list cached: {len(df)} rows -> {cache}")
    return df


def load_frs_nj(out_dir: Path, use_frs: bool) -> Tuple[pd.DataFrame, pd.DataFrame]:
    empty = pd.DataFrame(columns=["PGM_SYS_ACRNM", "PGM_SYS_ID", "REGISTRY_ID", "PRIMARY_NAME",
                                  "LOCATION_ADDRESS", "CITY_NAME", "COUNTY_NAME", "STATE_CODE", "POSTAL_CODE"])
    empty_f = pd.DataFrame(columns=["REGISTRY_ID", "LATITUDE_MEASURE", "LONGITUDE_MEASURE"])
    if not use_frs:
        return empty, empty_f
    cache_l, cache_f = out_dir / "frs_nj_program_links.csv", out_dir / "frs_nj_facilities.csv"
    if cache_l.exists() and cache_f.exists():
        links = pd.read_csv(cache_l, dtype=str).fillna("")
        facs = pd.read_csv(cache_f, dtype=str).fillna("")
        logger.info(f"FRS NJ cache: {len(links)} program links, {len(facs)} facilities")
        return links, facs
    if not FRS_LINKS.exists() or not FRS_FACS.exists():
        logger.warning(f"FRS extracts not found under {FRS_LINKS.parent}; skipping FRS lane")
        return empty, empty_f
    logger.info("Reading FRS extracts (NJ subset; a minute or two)…")
    keep = []
    for ch in pd.read_csv(FRS_LINKS, dtype=str, chunksize=1_000_000, usecols=list(empty.columns)):
        keep.append(ch[ch["STATE_CODE"] == "NJ"])
    links = pd.concat(keep).fillna("")
    facs_all = pd.read_csv(FRS_FACS, dtype=str, usecols=["REGISTRY_ID", "FAC_STATE", "LATITUDE_MEASURE", "LONGITUDE_MEASURE"])
    facs = facs_all[facs_all["FAC_STATE"] == "NJ"][["REGISTRY_ID", "LATITUDE_MEASURE", "LONGITUDE_MEASURE"]].fillna("")
    links.to_csv(cache_l, index=False)
    facs.to_csv(cache_f, index=False)
    logger.success(f"FRS NJ subset cached: {len(links)} program links, {len(facs)} facilities")
    return links, facs


# ----------------------------------------------------------------------------
# Target building
# ----------------------------------------------------------------------------
def dc_name_signal(name: str, extra_terms: List[str]) -> bool:
    n = str(name or "")
    if DC_NAME_RE.search(n):
        return True
    nn = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 &-]", " ", n.lower()))
    return any(t in nn for t in extra_terms)


def build_targets(echo: pd.DataFrame, frs_links: pd.DataFrame, frs_facs: pd.DataFrame) -> List[Dict[str, str]]:
    lbl = dc_targets.load_combined()
    lbl = lbl[lbl["state"].astype(str).str.upper() == "NJ"].copy() if len(lbl) else lbl
    terms = [t.lower() for t in dc_targets.entity_terms("NJ") if len(t) >= 6 and t.lower() not in NJ_TERM_STOPLIST]

    # Pre-compute agency keys.
    echo = echo.copy()
    echo["_strict"], echo["_loose"] = zip(*echo["AIRStreet"].map(addr_keys))
    echo["_pi"] = echo["SourceID"].map(pi_from_source_id)
    frs = frs_links.copy()
    if len(frs):
        frs["_strict"], frs["_loose"] = zip(*frs["LOCATION_ADDRESS"].map(addr_keys))
        frs = frs.merge(frs_facs, on="REGISTRY_ID", how="left").fillna("")
    echo_by_reg = {r: i for i, r in enumerate(echo["RegistryID"]) if r}

    targets: List[Dict[str, str]] = []
    matched_echo_idx: Set[int] = set()

    def echo_row_dict(x, method, conf, notes="") -> Dict[str, str]:
        return dict(agency_system="ECHO-ICIS-Air", agency_id=x["SourceID"], njems_pi_id=x["_pi"],
                    agency_name=x["AIRName"], agency_street=x["AIRStreet"], agency_city=x["AIRCity"],
                    agency_county=x["AIRCounty"], agency_naics=x["AIRNAICS"], air_classification=x["AIRClassification"],
                    air_status=x["AIRStatus"], air_programs=x["AIRPrograms"], frs_registry_id=x["RegistryID"],
                    match_method=method, match_confidence=conf, notes=notes)

    def frs_row_dict(x, method, conf, notes="") -> Dict[str, str]:
        pi = pi_from_source_id(x["PGM_SYS_ID"]) if x["PGM_SYS_ACRNM"] == "AIR" else ""
        return dict(agency_system=f"FRS-{x['PGM_SYS_ACRNM']}", agency_id=x["PGM_SYS_ID"], njems_pi_id=pi,
                    agency_name=x["PRIMARY_NAME"], agency_street=x["LOCATION_ADDRESS"], agency_city=x["CITY_NAME"],
                    agency_county=x["COUNTY_NAME"], agency_naics="", air_classification="", air_status="",
                    air_programs="", frs_registry_id=x["REGISTRY_ID"], match_method=method,
                    match_confidence=conf, notes=notes)

    def add(base: Dict[str, str], rec: Dict[str, str]) -> None:
        row = {k: "" for k in TARGET_FIELDS}
        row.update(base)
        row.update(rec)
        targets.append(row)

    def match_address(base: Dict[str, str], address: str, city: str, lat, lon) -> int:
        strict, loose = addr_keys(address)
        n_found = 0
        if not strict:
            return 0
        # ECHO
        for i, x in echo.iterrows():
            key_hit = x["_strict"] == strict
            loose_hit = (not key_hit) and x["_loose"] == loose
            if not (key_hit or loose_hit):
                continue
            d = haversine_km(lat, lon, x["FacLat"], x["FacLong"])
            geo_ok = d is not None and d <= 5.0
            city_ok = cities_agree(city, x["AIRCity"])
            if key_hit and (city_ok or geo_ok):
                signal = dc_name_signal(x["AIRName"], terms) or str(x["AIRNAICS"]).startswith(DC_NAICS_PREFIXES)
                conf = "high" if signal else "medium"
                method = "address_exact" + ("+name" if dc_name_signal(x["AIRName"], terms) else "")
            elif loose_hit and d is not None and d <= 1.0:
                conf, method = "low", "address_loose"
            else:
                continue
            matched_echo_idx.add(i)
            add(base, echo_row_dict(x, method, conf, f"geo_km={d:.1f}" if d is not None else ""))
            n_found += 1
        # FRS (non-air programs give addresses / stormwater construction records; AIR rows duplicate ECHO)
        if len(frs):
            hits = frs[(frs["_strict"] == strict)]
            for _, x in hits.iterrows():
                if x["PGM_SYS_ACRNM"] == "AIR":
                    continue  # same record as ECHO
                d = haversine_km(lat, lon, x.get("LATITUDE_MEASURE"), x.get("LONGITUDE_MEASURE"))
                if not (cities_agree(city, x["CITY_NAME"]) or (d is not None and d <= 5.0)):
                    continue
                signal = dc_name_signal(x["PRIMARY_NAME"], terms)
                conf = "high" if signal else "medium"
                note = "no ICIS-Air record at this address" if n_found == 0 else ""
                # link to an ECHO air row through the FRS registry id if present
                j = echo_by_reg.get(x["REGISTRY_ID"])
                if j is not None and j not in matched_echo_idx:
                    xe = echo.iloc[j]
                    matched_echo_idx.add(j)
                    add(base, echo_row_dict(xe, "frs_registry_link", conf, f"linked via FRS {x['PGM_SYS_ACRNM']} {x['PGM_SYS_ID']}"))
                    n_found += 1
                add(base, frs_row_dict(x, "address_exact" + ("+name" if signal else ""), conf, note))
                n_found += 1
        return n_found

    # ---- 1. LBL map rows -----------------------------------------------------
    tid = 0
    for _, r in lbl.iterrows():
        tid += 1
        base = dict(target_id=f"LBL-{tid:03d}", dc_operator=str(r.get("operator") or r.get("company") or ""),
                    dc_facility_name=str(r.get("facility_name") or ""), dc_address=str(r.get("address") or ""),
                    dc_city=str(r.get("city") or ""), dc_source="lbl_map", lbl_status=str(r.get("status") or ""),
                    lbl_it_load_mw=str(r.get("it_load_mw") or ""), dc_lat=str(r.get("latitude") or ""),
                    dc_lon=str(r.get("longitude") or ""))
        n = match_address(base, base["dc_address"], base["dc_city"], r.get("latitude"), r.get("longitude"))
        if n == 0:
            add(base, dict(match_method="none", match_confidence="",
                           notes="no ECHO/FRS record at this address — look up by address in DocMiner or include in OPRA by address"))

    # ---- 2. curated NJ list --------------------------------------------------
    for k, c in enumerate(CURATED_TARGETS, 1):
        base = dict(target_id=f"CUR-{k:02d}", dc_operator=c["operator"], dc_facility_name=c["facility"],
                    dc_address=c["address"], dc_city=c["city"], dc_source="curated")
        n = 0
        for pi in c["pis"]:
            hits = echo[echo["_pi"] == pi]
            for i, x in hits.iterrows():
                matched_echo_idx.add(i)
                add(base, echo_row_dict(x, "curated_pi", "high", c["notes"]))
                n += 1
        if c["address"]:
            n += match_address(base, c["address"], c["city"], None, None)
        if n == 0:
            add(base, dict(match_method="curated_note", match_confidence="", notes=c["notes"]))

    # ---- 3. ECHO-only: name / NAICS signals not already matched ------------
    for i, x in echo.iterrows():
        if i in matched_echo_idx:
            continue
        name_sig = dc_name_signal(x["AIRName"], terms)
        naics = str(x["AIRNAICS"])
        if naics.startswith(WAREHOUSE_NAICS_PREFIXES):
            continue
        if name_sig:
            method, conf = "echo_name_pattern", "medium"
        elif naics.startswith(("518210", "541513", "518111")):
            method, conf = "echo_naics_only", "low"
        else:
            continue
        tid += 1
        base = dict(target_id=f"ECHO-{tid:03d}", dc_operator="", dc_facility_name=x["AIRName"], dc_address=x["AIRStreet"],
                    dc_city=x["AIRCity"], dc_source="echo_only", dc_lat=x["FacLat"], dc_lon=x["FacLong"])
        add(base, echo_row_dict(x, method, conf, "enterprise / carrier data center not in LBL map" if name_sig else "NAICS-only candidate"))
    logger.success(f"targets: {len(targets)} rows; distinct PIs = {len({t['njems_pi_id'] for t in targets if t['njems_pi_id']})}")
    return targets


def write_targets(targets: List[Dict[str, str]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TARGET_FIELDS, extrasaction="ignore")
        w.writeheader()
        for t in targets:
            w.writerow(t)
    logger.success(f"Target list written: {path} ({len(targets)} rows)")


# ----------------------------------------------------------------------------
# Portal probe lane
# ----------------------------------------------------------------------------
def polite_get(session: requests.Session, url: str, delay: float, timeout: int, retries: int = 1) -> Tuple[Optional[requests.Response], str]:
    result = "error"
    r = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            result = classify_nj_response(r)
        except requests.RequestException as e:
            logger.warning(f"{url}: {type(e).__name__}: {e}")
            result = "error"
        if result == "ok":
            return r, result
        if attempt < retries:
            time.sleep(delay * 8 * (attempt + 1))
    return r, result


def probe_portals(session: requests.Session, out_dir: Path, targets: List[Dict[str, str]], index: IndexWriter,
                  pdf_dir: Path, delay: float, timeout: int, do_download: bool, budget: Dict[str, int]) -> Dict[str, str]:
    probe = IndexWriter(out_dir / "portal_access_probe.csv", PROBE_FIELDS)
    results: Dict[str, str] = {}
    snap_dir = out_dir / "portal_snapshots"
    for name, url in (("dataminer", DATAMINER_URL), ("dataminer_air", DATAMINER_AIR_URL),
                      ("docminer", DOCMINER_URL), ("boss_public_notices", BOSS_NOTICES_URL)):
        r, res = polite_get(session, url, delay, timeout)
        results[name] = res
        detail = ""
        if r is not None and res == "ok":
            snap_dir.mkdir(exist_ok=True)
            snap = snap_dir / f"{name}_{dt.datetime.now():%Y%m%d_%H%M%S}.html"
            snap.write_text(r.text, encoding="utf-8")
            detail = f"snapshot={snap.name}"
        probe.append(dict(probed_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), url=url,
                          http_status=(r.status_code if r is not None else ""), result=res,
                          bytes=(len(r.content) if r is not None else ""), detail=detail))
        logger.info(f"portal probe {name}: {res}")
        time.sleep(delay)
        if name == "boss_public_notices" and res == "ok" and r is not None:
            harvest_notice_page(session, r.text, targets, index, pdf_dir, delay, timeout, do_download, budget)
    if results.get("dataminer") == "ok":
        logger.warning("DataMiner answered without a challenge — the report parser is not implemented (the portal was "
                       "unreachable while this script was written); HTML snapshot saved under portal_snapshots/ "
                       "for building it.")
    return results


def harvest_notice_page(session, html: str, targets, index: IndexWriter, pdf_dir: Path, delay: float, timeout: int,
                        do_download: bool, budget: Dict[str, int]) -> None:
    """Index every PDF linked from the BOSS public-notice page; download target matches."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    target_pis = {t["njems_pi_id"] for t in targets if t["njems_pi_id"]}
    pi_targets: Dict[str, List[str]] = {}
    for t in targets:
        if t["njems_pi_id"]:
            pi_targets.setdefault(t["njems_pi_id"], []).append(t["target_id"])
    links = [(a.get_text(" ", strip=True), a["href"]) for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()]
    logger.info(f"BOSS public-notice page: {len(links)} PDF links")
    for text, href in links:
        url = requests.compat.urljoin(BOSS_NOTICES_URL, href)
        fname = url.rsplit("/", 1)[-1]
        m = re.match(r"^0*(\d{1,6})-([a-z]{3}\d{6})-(.+)\.pdf$", fname, re.I)
        pi = str(int(m.group(1))) if m else ""
        act = m.group(2).upper() if m else ""
        kind = m.group(3).lower() if m else "notice"
        name_hit = DC_NAME_RE.search(text or "") is not None
        if pi in target_pis or name_hit:
            fetch_document(session, url, pi, act, kind, pi_targets.get(pi, ["name-hit"]), text, "boss_page",
                           index, pdf_dir, delay, timeout, do_download, budget)


# ----------------------------------------------------------------------------
# Public-notice PDF probe lane
# ----------------------------------------------------------------------------
def fetch_document(session, url: str, pi: str, activity: str, kind: str, target_ids: List[str], facility: str,
                   lane: str, index: IndexWriter, pdf_dir: Path, delay: float, timeout: int, do_download: bool,
                   budget: Dict[str, int], head_first: bool = False) -> str:
    done = {(r["url"]): r for r in index.rows}
    prev = done.get(url)
    if prev and prev["status"] == "ok" and prev["pdf_path"] and Path(prev["pdf_path"]).exists():
        return "ok"
    if prev and prev["status"] == "missing" and not budget.get("reprobe_missing"):
        return "missing"
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", f"nj_{pi or 'x'}_{activity or 'na'}_{kind}")
    dest = pdf_dir / f"{stem}.pdf"
    row = dict(lane=lane, target_ids=";".join(target_ids), njems_pi_id=pi, facility_name=facility,
               activity_number=activity, doc_kind=kind, url=url, fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    try:
        if head_first:
            h = session.head(url, timeout=timeout, allow_redirects=True)
            time.sleep(delay)
            if h.status_code == 404:
                row.update(http_status=404, status="missing")
                index.append(row)
                return "missing"
            if h.status_code >= 400 or "pdf" not in h.headers.get("Content-Type", "").lower():
                row.update(http_status=h.status_code, status=classify_nj_response(h))
                index.append(row)
                return row["status"]
        if not do_download:
            row.update(http_status="", status="found_not_downloaded")
            index.append(row)
            return "found"
        if budget["limit"] is not None and budget["downloaded"] >= budget["limit"]:
            row.update(status="skipped_limit")
            index.append(row)
            return "limit"
        r = session.get(url, timeout=timeout * 3)
        time.sleep(delay)
        if r.status_code == 404:
            row.update(http_status=404, status="missing")
        elif r.content.startswith(b"%PDF"):
            dest.write_bytes(r.content)
            budget["downloaded"] += 1
            row.update(http_status=r.status_code, status="ok", pdf_path=str(dest), bytes=len(r.content))
            logger.success(f"[{budget['downloaded']}] {dest.name} ({len(r.content)//1024} KB)")
        else:
            row.update(http_status=r.status_code, status=classify_nj_response(r))
    except requests.RequestException as e:
        row.update(status=f"error:{type(e).__name__}")
        logger.warning(f"{url}: {e}")
    index.append(row)
    return row["status"]


def probe_notice_pdfs(session, targets, index: IndexWriter, pdf_dir: Path, years: List[int], seq_max: int,
                      all_pis: bool, explicit_pis: List[str], delay: float, timeout: int, do_download: bool,
                      budget: Dict[str, int]) -> None:
    by_pi: Dict[str, Dict[str, object]] = {}
    for t in targets:
        pi = t["njems_pi_id"]
        if not pi:
            continue
        d = by_pi.setdefault(pi, {"ids": [], "name": t["agency_name"], "cls": t["air_classification"], "curated": False})
        d["ids"].append(t["target_id"])
        if t["dc_source"] == "curated" or t["match_method"] == "curated_pi":
            d["curated"] = True
    def eligible(pi, d) -> bool:
        if explicit_pis:
            return pi in explicit_pis
        if all_pis:
            return True
        cls = str(d["cls"]).lower()
        return d["curated"] or "major" in cls or "80%" in cls
    chosen = {pi: d for pi, d in by_pi.items() if eligible(pi, d)}
    logger.info(f"public-notice probe: {len(chosen)} PIs x {len(years)} years x {seq_max} seq x 2 kinds (+1)")
    for pi, d in sorted(chosen.items()):
        urls = [(f"draft-permit-{pi}.pdf", "", "draftpermit")]
        for y in years:
            for kind in ("bop", "pcp"):
                for n in range(1, seq_max + 1):
                    act = f"{kind}{y % 100:02d}{n:04d}"
                    urls.append((f"{int(pi):05d}-{act}-draftpermit.pdf", act.upper(), "draftpermit"))
        found = 0
        for fname, act, kind in urls:
            st = fetch_document(session, NOTICE_PDF_BASE + fname, pi, act, kind, d["ids"], str(d["name"]),
                                "notice_probe", index, pdf_dir, delay, timeout, do_download, budget, head_first=True)
            if st in ("ok", "found"):
                found += 1
            if st == "limit":
                return
            if st in ("incident", "js-stub"):
                logger.error("dep.nj.gov started challenging static uploads — stopping the probe lane")
                return
        logger.info(f"PI {pi} ({d['name'][:40]}): {found} public-notice documents")


# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets-only", action="store_true", help="Build the target list and stop (no NJDEP requests)")
    ap.add_argument("--skip-portal-probe", action="store_true")
    ap.add_argument("--skip-notice-probe", action="store_true")
    ap.add_argument("--refresh-echo", action="store_true", help="Re-download the ECHO NJ facility list")
    ap.add_argument("--no-frs", action="store_true", help="Skip the local FRS extracts")
    ap.add_argument("--years", default="2018-2026", help="Year range for public-notice URL probes")
    ap.add_argument("--seq-max", type=int, default=2, help="Max permit-activity sequence number per year to probe")
    ap.add_argument("--probe-all-pis", action="store_true", help="Probe every target PI (default: majors, 80%% SM, curated)")
    ap.add_argument("--probe-pis", default="", help="Comma-separated PIs to probe instead of the default set")
    ap.add_argument("--reprobe-missing", action="store_true", help="Re-check URLs previously recorded as missing")
    ap.add_argument("--limit", type=int, default=None, help="Stop after downloading this many new PDFs")
    ap.add_argument("--max-pdfs", type=int, default=800, help="Hard cap on PDFs downloaded per run")
    ap.add_argument("--no-download", action="store_true", help="Index only")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between NJDEP requests")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = ap.parse_args()

    out_dir: Path = args.output_dir.expanduser().resolve()
    pdf_dir = out_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    logger.add(out_dir / "nj_dep_dc_permits.log", level="DEBUG", rotation="10 MB", enqueue=True)

    session = make_session()
    echo = load_echo_nj(out_dir, session, args.refresh_echo, args.timeout)
    frs_links, frs_facs = load_frs_nj(out_dir, not args.no_frs)
    targets = build_targets(echo, frs_links, frs_facs)
    write_targets(targets, out_dir / "nj_dc_permit_targets.csv")
    if args.targets_only:
        return 0

    index = IndexWriter(out_dir / "nj_dep_dc_permits_index.csv", INDEX_FIELDS)
    logger.info(f"Existing index rows: {len(index.rows)}")
    limit = args.max_pdfs if args.limit is None else min(args.limit, args.max_pdfs)
    budget = {"limit": limit, "downloaded": 0, "reprobe_missing": args.reprobe_missing}
    nj = make_session()

    if not args.skip_portal_probe:
        probe_portals(nj, out_dir, targets, index, pdf_dir, args.delay, args.timeout, not args.no_download, budget)
    if not args.skip_notice_probe:
        y0, y1 = (int(v) for v in args.years.split("-"))
        pis = [p.strip() for p in args.probe_pis.split(",") if p.strip()]
        probe_notice_pdfs(nj, targets, index, pdf_dir, list(range(y0, y1 + 1)), args.seq_max, args.probe_all_pis,
                          pis, args.delay, args.timeout, not args.no_download, budget)
    n_ok = sum(1 for r in index.rows if r["status"] == "ok")
    logger.success(f"Done: {budget['downloaded']} new PDFs this run; index has {len(index.rows)} rows, {n_ok} documents on disk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
