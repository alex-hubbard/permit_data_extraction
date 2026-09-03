#!/usr/bin/env python3
"""Download Tennessee air-permit documents for data centers.

Two agencies issue air permits in Tennessee:

TDEC Division of Air Pollution Control (statewide except Memphis/Shelby,
Nashville/Davidson, Knox, Hamilton counties which run local programs)
---------------------------------------------------------------------
Oracle APEX app 19031 at https://dataviewers.tdec.tn.gov/dataviewers/ (no
login; session id comes from the first page load):

  f?p=19031:34100:<sess>:CSV::RIR:            Active facilities CSV (9,191 rows;
                                              Facility ID, name, SIC, address)
  f?p=19031:34001:<sess>:CSV::RIR:            Issued permits CSV (8,475 rows)
  f?p=19031:2:<sess>::NO:RIR:IR_FACILITY_ID:<fid>
                                              eFiles interactive report filtered
                                              to one facility (IRC_<col>:<txt>
                                              = "contains" filter on DESCRIPTION
                                              / SITE / PRIMARY_NAME for discovery)
  POST wwv_flow.ajax  p_request=PLUGIN=<ajaxIdentifier> p_widget_name=worksheet
       p_widget_mod=ACTION p_widget_action=SEARCH p_widget_num_return=1000
       x01=<worksheet id> x02=<report id>      returns the filtered report as
                                              HTML with all rows (paged >1000)
  BGAPC.GET_DOCUMENTS?p_file=<id>             the document (PDF, sometimes
                                              .msg/.docx; extension taken from
                                              Content-Disposition)

Facilities are matched four ways (recorded in ``match_reason``): SIC 737x
(computer/data processing), operator-name aliases (dc_targets + TN list
below), LBL data-center-map street addresses, and eFiles "contains" sweeps for
data-center keywords / operator names (discovery hits outside a matched
facility download only the matching rows).

Shelby County Health Department APC (Memphis — xAI Colossus etc.)
-------------------------------------------------------------------
No permit database; permit actions are posted as PDFs in the CivicPlus
DocumentCenter of shelbytnhealth.com, and only current notices stay linked
from /310/Air-Pollution-Control-Public-Notices. As of 2026-09 the site answers
every request from this network with a 302 to itself (curl, headless Chrome
and the WebFetch service all fail with a redirect loop / 403), so the live
lane cannot run. With ``--shelby-source wayback`` (default ``auto`` = try live,
fall back) the script enumerates the Internet Archive's copies instead:

  http://web.archive.org/cdx/search/cdx?url=shelbytnhealth.com/DocumentCenter/View/*
  https://web.archive.org/web/<timestamp>id_/<original url>   (raw file)

Archived copies of the notices page supply link text for files posted
without a slug. Documents naming a data-center facility (SHELBY_DC_FACILITIES,
SHELBY_DC_NAME_RE) plus the monthly public-notice lists are downloaded by
default; ``--shelby-all`` takes every archived permit-action document
(including TVA Allen, facility 01280).

Output layout
-------------
  <RAW_DATA_DIR>/tn_dc_permits/
    pdfs/tdec_<facility>_<permit>_<filetype>_<p_file>.pdf
    pdfs/schd_<docid>_<slug>.pdf
    tn_dc_permits_index.csv          one row per document, appended as we go
    tn_dc_matched_facilities.csv     TDEC facilities selected and why
    tdec_apc_facilities.csv / tdec_apc_permits.csv   full TDEC exports
    tn_dc_permits.log

Resumable: documents already listed in the index with status "ok" (and a file
on disk) are skipped; re-running only fetches what is missing.
"""

from __future__ import annotations

import argparse
import csv
import html as htmllib
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote, unquote

import pandas as pd
import requests
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

import dc_targets  # noqa: E402

OUTPUT_DIR = RAW_DATA_DIR / "tn_dc_permits"
INDEX_NAME = "tn_dc_permits_index.csv"

TDEC_BASE = "https://dataviewers.tdec.tn.gov/dataviewers/"
TDEC_APP = "19031"
TDEC_PAGE_PERMITS, TDEC_PAGE_FACILITIES, TDEC_PAGE_EFILES = "34001", "34100", "2"

SHELBY_HOST = "https://www.shelbytnhealth.com"
SHELBY_NOTICES = SHELBY_HOST + "/310/Air-Pollution-Control-Public-Notices"
WAYBACK_CDX = "http://web.archive.org/cdx/search/cdx"
WAYBACK_RAW = "https://web.archive.org/web/{ts}id_/{url}"

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/150.0 Safari/537.36"}

# Operator / permit-entity names specific to Tennessee data centers (LBL map,
# press reports, TDEC facility list). Matched on word boundaries against TDEC
# facility, permittee and eFiles site names.
TN_ALIASES = [
    # hyperscale permit entities
    "Foxman", "Google", "Alphabet", "Woolhawk", "Stadion", "Meta Platforms", "Facebook",
    "X.AI", "xAI", "CTC Property", "Colossus", "Oracle America", "Oracle Corp",
    "Microsoft", "Amazon Data Services", "Vadata", "Amazon Web Services",
    # colocation / enterprise
    "Flexential", "Tier Point", "TierPoint", "Compass Datacenters", "DataBank",
    "Mapletree", "EdgeConneX", "Expedient", "5C Data", "DC BLOX", "DCBLOX",
    "Peace Communications", "Peacecom", "Lumen", "CenturyLink", "Level 3", "Zayo",
    "Cogent Communications", "Windstream", "H5 Data", "365 Data", "Centersquare",
    "Archer Datacenters", "Radius DC", "Tenhats", "Webservio", "Litewire",
    "204 Red Road", "Webnet", "Data One Corp", "Data Suites", "Aeneas",
    "Aphorio", "Carter Validus", "Merkle Standard", "Oppidan", "Airnet",
    "Electric Power Board of Chattanooga", "Morristown Utility",
    # crypto / HPC
    "CleanSpark", "Exponential Digital", "Bitdeer", "ExoticRidge", "Reddog",
    "Core Scientific", "Applied Digital", "Crusoe", "CoreWeave",
]
# dc_targets aliases too short/generic for a TN word-boundary search.
DROP_TERMS = {"amazon", "design llc", "switch", "ntt", "qts", "iren", "tract capital",
              "cologix", "sabey", "novva", "skybox", "yondr", "corscale", "lambda labs"}

# eFiles "contains" sweeps (column -> phrases). "mining" alone floods on coal.
DISCOVERY_SWEEPS = {
    "DESCRIPTION": ["data center", "datacenter", "colocation", "hyperscale",
                    "crypto", "bitcoin", "blockchain", "server farm"],
    "SITE": ["data center", "datacenter", "colocation", "crypto", "bitcoin", "blockchain"],
    "PRIMARY_NAME": ["data center", "datacenter", "colocation", "crypto", "bitcoin"],
}
DC_SIC_PREFIXES = ("737",)
# Name/permittee matches are ignored for these (site contractors, cell towers).
EXCLUDE_NAME_RE = re.compile(r"excavat|contractor|concrete|crush|asphalt|paving|\btower|wireless|cellular|\bpcs\b", re.IGNORECASE)

# eFiles file types that are not air-permit material (skipped by default).
DEFAULT_SKIP_TYPES = [
    "Fee Correspondence", "Fee Correspondence - AEAR", "Certified Mail Receipt", "Image",
    "Notice of Demolition/Asbestos Renovation", "Asbestos Survey/Support Documentation",
    "Inspection Report - Asbestos",
]
# Download order when the cap bites: permits and their support first.
TYPE_PRIORITY = [
    "Permit", "Old Permit", "Draft/Proposed Permit", "Statement of Basis",
    "Public Notice Document", "Emission Summary", "Calculations", "Addendum",
    "Notice of Authorization (NOA)", "Notice of Intent (NOI)", "NOC", "Application",
    "Letter", "Report", "Inspection Report", "Emission Statement – NOx/VOC",
    "Start-Up Notification", "Public Notice Comments", "Form", "Email",
]

# Shelby County facility numbers known to be data centers (01280 is TVA's
# Allen Combined Cycle Plant - the gas plant serving Memphis/xAI load, kept
# only with --shelby-all).
SHELBY_DC_FACILITIES = {
    "01156": "CTC Property LLC (xAI Colossus 1, 3231 Paul R. Lowry Rd) - gas turbines",
}
SHELBY_RELATED_FACILITIES = {"01280": "TVA Allen Combined Cycle Plant (serves xAI/Memphis load)"}
SHELBY_DC_NAME_RE = re.compile(
    r"CTC[- ]Property|\bxAI\b|X\.AI|Colossus|\b5C\b|MEM0?1|data ?cent|EdgeConneX|DataBank|"
    r"Expedient|Merkle|Oppidan|Lumen|Cogent|Verizon|Webnet|Aphorio|Carter Validus|turbine", re.IGNORECASE)
SHELBY_FAC_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
SHELBY_NOTICE_RE = re.compile(r"PUBLIC[- ]NOTICE|TV-Public-Notice|Air Board", re.IGNORECASE)
SHELBY_ACTION_RE = re.compile(r"permit|PAE|draft|renewal|construction|notice|modification|\dTV|\dPC|\dSM", re.IGNORECASE)

INDEX_FIELDS = [
    "agency", "source", "match_reason", "facility_id", "facility_name", "site_name",
    "permit_number", "permit_type", "county", "efo", "file_type", "description",
    "effective_date", "doc_id", "doc_url", "archive_url", "archive_timestamp",
    "pdf_path", "content_type", "bytes", "status", "fetched_at",
]


# --------------------------------------------------------------------------- utils
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_stem(s: str, n: int = 90) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")
    return s[:n]


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def norm_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 &.-]", " ", str(s).lower())
    return re.sub(r"\s+", " ", s).strip()


def term_regex(terms: Iterable[str]) -> Optional[re.Pattern]:
    parts = [r"\b" + re.escape(norm_name(t)) + r"\b" for t in terms if norm_name(t)]
    return re.compile("|".join(parts)) if parts else None


def address_key(addr: str) -> Optional[str]:
    """'1000 Boolean Drive, Suite 2' -> '1000 boolean' for fuzzy address joins."""
    a = re.sub(r"[^a-z0-9 ]", " ", str(addr).lower())
    a = re.sub(r"\b(north|south|east|west)\b", lambda m: m.group(1)[0], a)
    a = re.sub(r"\b(street|st|road|rd|drive|dr|avenue|ave|parkway|pkwy|lane|ln|boulevard|"
               r"blvd|pike|way|highway|hwy|court|ct|place|pl|suite|ste|circle|cir|loop|lp)\b", " ", a)
    m = re.match(r"\s*(\d+)\s+([a-z0-9]+)", a)
    return f"{m.group(1)} {m.group(2)}" if m else None


class Index:
    """Append-only CSV index; rows already fetched are skipped on resume."""

    def __init__(self, path: Path):
        self.path = path
        self.done: Set[Tuple[str, str]] = set()
        self.seen: Set[Tuple[str, str]] = set()
        self.n_ok = 0
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    key = (r.get("agency", ""), r.get("doc_id", ""))
                    self.seen.add(key)
                    if r.get("status") == "ok" and r.get("pdf_path") and Path(r["pdf_path"]).exists():
                        self.done.add(key)
                        self.n_ok += 1
            logger.info(f"Index has {len(self.seen)} rows, {len(self.done)} downloaded")
        else:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=INDEX_FIELDS).writeheader()

    def is_done(self, agency: str, doc_id: str) -> bool:
        return (agency, doc_id) in self.done

    def append(self, row: Dict[str, str]) -> None:
        row = {k: row.get(k, "") for k in INDEX_FIELDS}
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=INDEX_FIELDS).writerow(row)
        self.seen.add((row["agency"], row["doc_id"]))
        if row["status"] == "ok":
            self.done.add((row["agency"], row["doc_id"]))
            self.n_ok += 1


def ext_from_headers(resp: requests.Response, content: bytes) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    if m:
        name = unquote(m.group(1)).strip()
        if "." in name:
            ext = name.rsplit(".", 1)[1].lower()
            if re.fullmatch(r"[a-z0-9]{2,5}", ext):
                return ext
    if content[:4] == b"%PDF":
        return "pdf"
    ct = resp.headers.get("Content-Type", "").lower()
    if "pdf" in ct:
        return "pdf"
    if "msword" in ct:
        return "doc"
    if "outlook" in ct or "vnd.ms-outlook" in ct:
        return "msg"
    return "bin"


def fetch_file(session: requests.Session, url: str, dest_stem: Path, timeout: int) -> Tuple[Optional[Path], str, int, str]:
    """GET url -> (path or None, content_type, nbytes, status)."""
    try:
        r = session.get(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return None, "", 0, f"error: {e}"
    if r.status_code != 200:
        return None, r.headers.get("Content-Type", ""), 0, f"http {r.status_code}"
    content = r.content
    ct = r.headers.get("Content-Type", "")
    if not content or (content[:4] != b"%PDF" and "text/html" in ct.lower()):
        return None, ct, len(content), "not a document (html)"
    ext = ext_from_headers(r, content)
    dest = dest_stem.with_suffix("." + ext)
    dest.write_bytes(content)
    return dest, ct, len(content), "ok"


# --------------------------------------------------------------------------- TDEC
class TDEC:
    def __init__(self, delay: float, timeout: int):
        self.delay, self.timeout = delay, timeout
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.inst = ""
        self._open()

    def _open(self) -> None:
        r = self.s.get(f"{TDEC_BASE}f?p={TDEC_APP}:{TDEC_PAGE_PERMITS}:::::", timeout=self.timeout)
        r.raise_for_status()
        m = re.search(r'name="p_instance"[^>]*value="(\d+)"', r.text)
        if not m:
            raise RuntimeError("TDEC DataViewer: no APEX session id in landing page")
        self.inst = m.group(1)
        logger.info(f"TDEC APEX session {self.inst}")

    def export_csv(self, page: str) -> pd.DataFrame:
        url = f"{TDEC_BASE}f?p={TDEC_APP}:{page}:{self.inst}:CSV::RIR:"
        r = self.s.get(url, timeout=self.timeout * 3)
        r.raise_for_status()
        if "text/csv" not in r.headers.get("Content-Type", ""):
            raise RuntimeError(f"TDEC page {page}: CSV export returned {r.headers.get('Content-Type')}")
        time.sleep(self.delay)
        return pd.read_csv(io.BytesIO(r.content), dtype=str).fillna("")

    def efiles(self, col: str, value: str, op: str = "IR") -> List[Dict[str, str]]:
        """All eFiles rows for a filter (IR_ = equals, IRC_ = contains)."""
        url = f"{TDEC_BASE}f?p={TDEC_APP}:{TDEC_PAGE_EFILES}:{self.inst}::NO:RIR:{op}_{col}:{quote(value, safe='')}"
        r = self.s.get(url, timeout=self.timeout)
        r.raise_for_status()
        h = r.text
        if 'name="p_instance"' not in h:
            logger.warning("TDEC session lost; reopening")
            self._open()
            return self.efiles(col, value, op)
        aid = htmllib.unescape(re.findall(r'"ajaxIdentifier":"([^"]+)"', h)[0]).encode().decode("unicode_escape")
        wsid = re.findall(r'_worksheet_id"[^>]*value="([^"]*)"', h)[0]
        rid = re.findall(r'_report_id"[^>]*value="([^"]*)"', h)[0]
        base = {
            "p_flow_id": TDEC_APP, "p_flow_step_id": TDEC_PAGE_EFILES, "p_instance": self.inst,
            "p_debug": "", "p_request": "PLUGIN=" + aid, "p_widget_name": "worksheet",
            "p_widget_mod": "ACTION", "x01": wsid, "x02": rid,
            "p_json": json.dumps({"pageItems": None, "salt": ""}),
        }
        rows: List[Dict[str, str]] = []
        min_row, page_size = 1, 1000
        while True:
            data = dict(base, p_widget_action="SEARCH" if min_row == 1 else "PAGE",
                        p_widget_num_return=str(page_size))
            if min_row > 1:
                data["p_widget_action_mod"] = f"pgR_min_row={min_row}max_rows={page_size}rows_fetched={page_size}"
            rr = self.s.post(f"{TDEC_BASE}wwv_flow.ajax", data=data, timeout=self.timeout * 2)
            rr.raise_for_status()
            t = rr.text
            got = 0
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
                tds = re.findall(r'<td[^>]*headers="([^"]+)"[^>]*>(.*?)</td>', tr, re.S)
                if not tds:
                    continue
                d = {k: strip_tags(v) for k, v in tds}
                m = re.search(r"p_file=(\d+)", tr)
                d["p_file"] = m.group(1) if m else ""
                rows.append(d)
                got += 1
            m = re.search(r"(\d[\d,]*) - (\d[\d,]*) of (\d[\d,]*)", strip_tags(t))
            total = int(m.group(3).replace(",", "")) if m else got
            time.sleep(self.delay)
            if got == 0 or min_row + got > total:
                break
            min_row += got
        return rows

    def doc_url(self, p_file: str) -> str:
        return f"{TDEC_BASE}BGAPC.GET_DOCUMENTS?p_file={p_file}"


def select_tdec_facilities(fac: pd.DataFrame, per: pd.DataFrame, terms: List[str]) -> Dict[str, List[str]]:
    """facility_id -> list of match reasons."""
    reasons: Dict[str, List[str]] = {}

    def add(fid: str, why: str) -> None:
        if fid and fid != "-":
            reasons.setdefault(fid, [])
            if why not in reasons[fid]:
                reasons[fid].append(why)

    # 1. SIC 737x
    for _, r in fac.iterrows():
        sic = str(r.get("SICCode", "")).split(".")[0]
        if sic.startswith(DC_SIC_PREFIXES):
            add(r["Facility ID"], f"sic:{sic}")

    # 2. operator names (facility primary name and permittee name)
    rx = term_regex(terms)
    if rx:
        for _, r in fac.iterrows():
            m = rx.search(norm_name(r["Primary Name"]))
            if m and not EXCLUDE_NAME_RE.search(r["Primary Name"]):
                add(r["Facility ID"], f"name:{m.group(0)}")
        for _, r in per.iterrows():
            m = rx.search(norm_name(r["Permittee Name"]))
            if m and not EXCLUDE_NAME_RE.search(r["Permittee Name"]):
                add(r["Facility ID"], f"permittee:{m.group(0)}")

    # 3. LBL map street addresses (same city)
    lbl = dc_targets.load_combined()
    lbl = lbl[lbl["state"].astype(str).str.upper() == "TN"]
    if len(lbl):
        lbl_keys = {}
        for _, r in lbl.iterrows():
            k = address_key(r.get("address", ""))
            if k:
                lbl_keys[(k, str(r.get("city", "")).lower().strip())] = f"{r.get('company', '')} / {r.get('facility_name', '')}"
        for _, r in fac.iterrows():
            k = address_key(r.get("Address", ""))
            if k:
                hit = lbl_keys.get((k, str(r.get("City", "")).lower().strip()))
                if hit:
                    add(r["Facility ID"], f"address:{hit}"[:120])
    return reasons


def run_tdec(args, index: Index, budget: Dict[str, int]) -> None:
    pdf_dir = OUTPUT_DIR / "pdfs"
    td = TDEC(args.delay, args.timeout)

    fac = td.export_csv(TDEC_PAGE_FACILITIES)
    per = td.export_csv(TDEC_PAGE_PERMITS)
    fac.to_csv(OUTPUT_DIR / "tdec_apc_facilities.csv", index=False)
    per.to_csv(OUTPUT_DIR / "tdec_apc_permits.csv", index=False)
    logger.info(f"TDEC exports: {len(fac)} facilities, {len(per)} issued permits")
    permit_type = {r["PermitNumber"]: r["Type"] for _, r in per.iterrows() if r.get("PermitNumber")}
    fac_by_id = {r["Facility ID"]: r for _, r in fac.iterrows()}

    terms = [t for t in dc_targets.entity_terms("TN") if t.lower() not in DROP_TERMS and len(t) >= 4]
    terms += TN_ALIASES
    if args.facility_ids:
        reasons = {f: ["cli"] for f in args.facility_ids.split(",") if f.strip()}
    else:
        reasons = select_tdec_facilities(fac, per, terms)

    # 4. eFiles discovery sweeps: keywords + operator aliases on site / name.
    discovery_rows: Dict[str, Dict[str, str]] = {}   # p_file -> row (facility '-' or unmatched)
    if not args.facility_ids and not args.no_discovery:
        sweeps = [(c, p) for c, ps in DISCOVERY_SWEEPS.items() for p in ps]
        sweeps += [(c, a) for a in TN_ALIASES for c in ("SITE", "PRIMARY_NAME")]
        for i, (col, phrase) in enumerate(sweeps, 1):
            try:
                rows = td.efiles(col, phrase, op="IRC")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"sweep {col} contains {phrase!r}: {e}")
                continue
            if not rows:
                continue
            # A facility whose *site or name* carries the term is a facility-level match;
            # description-only hits are row-level unless the facility is already matched.
            fids = {r["FACILITY_ID"] for r in rows}
            logger.info(f"[{i}/{len(sweeps)}] {col} contains {phrase!r}: {len(rows)} rows, facilities {sorted(fids)[:8]}")
            is_keyword = phrase in DISCOVERY_SWEEPS.get(col, [])
            alias_rx = None if is_keyword else term_regex([phrase])
            for r in rows:
                fid = r["FACILITY_ID"]
                why = f"sweep:{col.lower()}~{phrase}"
                name_ok = bool(alias_rx and alias_rx.search(norm_name(r["PRIMARY_NAME"]))
                               and not EXCLUDE_NAME_RE.search(r["PRIMARY_NAME"]))
                if fid != "-" and fid in fac_by_id and (name_ok or (is_keyword and col in ("SITE", "PRIMARY_NAME"))):
                    # Facility-level: the facility itself is named for the operator / keyword.
                    if not EXCLUDE_NAME_RE.search(fac_by_id[fid]["Primary Name"]):
                        reasons.setdefault(fid, [])
                        if why not in reasons[fid]:
                            reasons[fid].append(why)
                elif (is_keyword or name_ok) and r["p_file"] and r["p_file"] not in discovery_rows:
                    # Row-level: keyword in the description, or an operator alias on a
                    # facility outside the active list. A site name alone is not enough
                    # (e.g. Hemlock Semiconductor's legacy permits sit under site "Foxman
                    # LLC" because Google bought the plant).
                    r["_reason"] = why
                    discovery_rows[r["p_file"]] = r
    # Drop the sweep rows that belong to facilities we download in full anyway.
    discovery_rows = {k: v for k, v in discovery_rows.items() if v["FACILITY_ID"] not in reasons}

    with open(OUTPUT_DIR / "tn_dc_matched_facilities.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["facility_id", "primary_name", "sic", "city", "county", "address", "match_reasons"])
        for fid, why in sorted(reasons.items()):
            r = fac_by_id.get(fid, {})
            w.writerow([fid, r.get("Primary Name", ""), r.get("SICCode", ""), r.get("City", ""),
                        r.get("County", ""), r.get("Address", ""), "; ".join(why)])
    logger.success(f"TDEC: {len(reasons)} matched facilities, {len(discovery_rows)} extra discovery rows")

    # Enumerate documents per facility.
    docs: List[Dict[str, str]] = []
    for i, (fid, why) in enumerate(sorted(reasons.items()), 1):
        try:
            rows = td.efiles("FACILITY_ID", fid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{i}/{len(reasons)}] eFiles for {fid}: {e}")
            continue
        name = fac_by_id.get(fid, {}).get("Primary Name", rows[0]["PRIMARY_NAME"] if rows else "")
        logger.info(f"[{i}/{len(reasons)}] {fid} {name[:50]!r}: {len(rows)} documents ({'; '.join(why)[:80]})")
        for r in rows:
            r["_reason"] = "; ".join(why)
            docs.append(r)
    docs.extend(discovery_rows.values())

    skip = {t.strip().lower() for t in args.skip_types.split(",") if t.strip()}
    keep = {t.strip().lower() for t in args.include_types.split(",") if t.strip()}
    prio = {t.lower(): i for i, t in enumerate(TYPE_PRIORITY)}

    def wanted(r: Dict[str, str]) -> bool:
        ft = r.get("FILE_TYPE", "").lower()
        if keep:
            return ft in keep
        return ft not in skip

    def sort_key(r: Dict[str, str]) -> Tuple[int, str]:
        try:
            d = datetime.strptime(r.get("EFFECTIVE_DATE", ""), "%b-%d-%Y")
            ds = d.strftime("%Y%m%d")
        except ValueError:
            ds = "00000000"
        return prio.get(r.get("FILE_TYPE", "").lower(), len(TYPE_PRIORITY)), "9" if not ds else "".join(str(9 - int(c)) for c in ds)

    seen_pf: Set[str] = set()
    todo = []
    for r in docs:
        pf = r.get("p_file", "")
        if not pf or pf in seen_pf:
            continue
        seen_pf.add(pf)
        if wanted(r):
            todo.append(r)
    todo.sort(key=sort_key)
    logger.info(f"TDEC: {len(seen_pf)} unique documents, {len(todo)} after type filter")

    n_new = 0
    for r in todo:
        pf = r["p_file"]
        row = {
            "agency": "TDEC APC", "source": "dataviewers.tdec.tn.gov", "match_reason": r.get("_reason", ""),
            "facility_id": r.get("FACILITY_ID", ""), "facility_name": r.get("PRIMARY_NAME", ""),
            "site_name": r.get("SITE", ""), "permit_number": r.get("PERMIT_NUMBER", "").replace("-", "") or "",
            "permit_type": permit_type.get(r.get("PERMIT_NUMBER", ""), ""), "county": r.get("COUNTY", ""),
            "efo": r.get("EFO", ""), "file_type": r.get("FILE_TYPE", ""), "description": r.get("DESCRIPTION", ""),
            "effective_date": r.get("EFFECTIVE_DATE", ""), "doc_id": pf, "doc_url": td.doc_url(pf),
        }
        if index.is_done("TDEC APC", pf):
            continue
        if args.no_download:
            row.update(status="indexed", fetched_at=now_iso())
            if ("TDEC APC", pf) not in index.seen:
                index.append(row)
            continue
        if budget["left"] <= 0:
            logger.warning("Download budget exhausted; remaining TDEC documents left for a later run")
            break
        stem = safe_stem(f"tdec_{row['facility_id']}_{row['permit_number'] or 'nopermit'}_{row['file_type']}_{pf}")
        path, ct, nbytes, status = fetch_file(td.s, row["doc_url"], pdf_dir / stem, args.timeout * 3)
        row.update(pdf_path=str(path) if path else "", content_type=ct, bytes=str(nbytes),
                   status=status, fetched_at=now_iso())
        index.append(row)
        budget["left"] -= 1
        n_new += 1
        if status == "ok":
            logger.info(f"  ok  {row['facility_id']} {row['file_type'][:22]:22} {row['effective_date']:11} {row['description'][:50]}")
        else:
            logger.warning(f"  {status}: {row['doc_url']}")
        time.sleep(args.delay)
    logger.success(f"TDEC lane done: {n_new} new downloads this run")


# --------------------------------------------------------------------------- Shelby
def shelby_live_blocked(session: requests.Session, timeout: int) -> bool:
    try:
        r = session.get(SHELBY_NOTICES, timeout=timeout, allow_redirects=False)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Shelby live site unreachable: {e}")
        return True
    loc = r.headers.get("Location", "")
    if r.status_code in (301, 302, 303, 307, 308) and loc.rstrip("/") == SHELBY_NOTICES.rstrip("/"):
        logger.warning("Shelby live site answers with a redirect to itself (302 loop) - blocked for this client")
        return True
    if r.status_code != 200:
        logger.warning(f"Shelby live site returned HTTP {r.status_code}")
        return True
    return False


def shelby_wayback_docs(session: requests.Session, timeout: int, delay: float) -> List[Dict[str, str]]:
    params = {
        "url": "shelbytnhealth.com/DocumentCenter/View/*", "output": "json",
        "fl": "timestamp,original,statuscode,mimetype", "filter": "statuscode:200",
        "collapse": "urlkey", "limit": "5000",
    }
    for attempt in range(4):
        try:
            r = session.get(WAYBACK_CDX, params=params, timeout=timeout * 2)
            if r.status_code == 200 and r.text.strip().startswith("["):
                break
            logger.warning(f"CDX attempt {attempt + 1}: HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"CDX attempt {attempt + 1}: {e}")
        time.sleep(delay * 5)
    else:
        raise RuntimeError("Wayback CDX unavailable")
    rows = r.json()[1:]
    best: Dict[str, Dict[str, str]] = {}
    for ts, orig, _, mime in rows:
        m = re.search(r"/DocumentCenter/View/(\d+)(?:/([^?#]*))?", orig, re.IGNORECASE)
        if not m:
            continue
        doc_id, slug = m.group(1), unquote(m.group(2) or "")
        cur = best.get(doc_id)
        if cur is None or ts > cur["ts"] or (not cur["slug"] and slug):
            best[doc_id] = {"doc_id": doc_id, "slug": slug or (cur or {}).get("slug", ""), "ts": ts,
                            "original": orig, "mime": mime}
    return list(best.values())


def shelby_notice_page_labels(session: requests.Session, timeout: int, delay: float) -> Dict[str, Tuple[str, str]]:
    """doc_id -> (link text, snapshot ts) from every distinct archived notices page."""
    import gzip
    params = {"url": SHELBY_NOTICES.replace("https://", ""), "output": "json",
              "fl": "timestamp,original,digest", "filter": "statuscode:200", "collapse": "digest", "limit": "200"}
    labels: Dict[str, Tuple[str, str]] = {}
    try:
        r = session.get(WAYBACK_CDX, params=params, timeout=timeout * 2)
        snaps = r.json()[1:] if r.status_code == 200 and r.text.strip().startswith("[") else []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"CDX for notices page: {e}")
        snaps = []
    logger.info(f"Shelby notices page: {len(snaps)} distinct archived versions")
    for ts, orig, _ in snaps:
        try:
            rr = session.get(WAYBACK_RAW.format(ts=ts, url=orig), timeout=timeout * 2)
            raw = rr.content
            try:
                raw = gzip.decompress(raw)
            except Exception:  # noqa: BLE001
                pass
            page = raw.decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"notices snapshot {ts}: {e}")
            continue
        for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
            u = htmllib.unescape(m.group(1))
            mm = re.search(r"/DocumentCenter/View/(\d+)", u, re.IGNORECASE)
            if not mm:
                continue
            text = strip_tags(m.group(2))
            if text and (mm.group(1) not in labels or len(text) > len(labels[mm.group(1)][0])):
                labels[mm.group(1)] = (text, ts)
        time.sleep(max(delay, 2.0))
    logger.info(f"Shelby notices page: {len(labels)} labelled DocumentCenter links")
    return labels


def run_shelby(args, index: Index, budget: Dict[str, int]) -> None:
    pdf_dir = OUTPUT_DIR / "pdfs"
    s = requests.Session()
    s.headers.update(HEADERS)
    mode = args.shelby_source
    if mode == "skip":
        return
    if mode in ("auto", "live"):
        if shelby_live_blocked(s, args.timeout):
            if mode == "live":
                logger.error("Shelby live lane blocked; rerun with --shelby-source wayback for the archived copies")
                return
            mode = "wayback"
        else:
            logger.error("Shelby live site reachable but no live scraper is implemented yet "
                         "(the site was unreachable when this script was written); use --shelby-source wayback")
            return

    docs = shelby_wayback_docs(s, args.timeout, args.delay)
    logger.info(f"Wayback lists {len(docs)} distinct Shelby DocumentCenter files")
    # Link text from archived copies of the notices page names the slug-less
    # files (e.g. /DocumentCenter/View/7173 = "SCHD Public Notice July 1, 2025").
    labels = shelby_notice_page_labels(s, args.timeout, args.delay)
    by_id = {d["doc_id"]: d for d in docs}
    for doc_id, (text, ts) in labels.items():
        d = by_id.setdefault(doc_id, {"doc_id": doc_id, "slug": "", "ts": ts,
                                      "original": f"{SHELBY_HOST}/DocumentCenter/View/{doc_id}", "mime": ""})
        d["label"] = text
    picked = []
    for d in by_id.values():
        text = f"{d['slug']} {d.get('label', '')}".strip()
        if not text:
            continue
        m = SHELBY_FAC_RE.search(text)
        fac = m.group(1) if m else ""
        is_notice = bool(SHELBY_NOTICE_RE.search(text))
        if fac in SHELBY_DC_FACILITIES or (SHELBY_DC_NAME_RE.search(text) and SHELBY_ACTION_RE.search(text)):
            reason = f"shelby_dc:{fac or 'name'}:{SHELBY_DC_FACILITIES.get(fac, text)[:40]}"
        elif is_notice:
            reason = "shelby_public_notice_list"
        elif args.shelby_all and (fac or SHELBY_ACTION_RE.search(text)) and not re.match(r"APC-?\d|APCV", d["slug"]):
            reason = (f"shelby_related:{fac}:{SHELBY_RELATED_FACILITIES[fac][:40]}" if fac in SHELBY_RELATED_FACILITIES
                      else f"shelby_permit_action:{fac or 'unknown'}")
        else:
            continue
        d["fac"], d["reason"], d["text"] = fac, reason, text
        picked.append(d)
    picked.sort(key=lambda d: (0 if d["reason"].startswith("shelby_dc") else 1, d["ts"]), reverse=False)
    logger.success(f"Shelby: {len(picked)} archived permit-action documents selected "
                   f"({sum(d['reason'].startswith('shelby_dc') for d in picked)} data-center)")

    n_new = 0
    for d in picked:
        doc_id = d["doc_id"]
        row = {
            "agency": "SCHD APC", "source": "web.archive.org", "match_reason": d["reason"],
            "facility_id": d["fac"], "facility_name": SHELBY_DC_FACILITIES.get(d["fac"], ""),
            "county": "Shelby", "description": d["text"], "doc_id": doc_id,
            "doc_url": d["original"], "archive_url": WAYBACK_RAW.format(ts=d["ts"], url=d["original"]),
            "archive_timestamp": d["ts"], "file_type": "Public Notice" if SHELBY_NOTICE_RE.search(d["text"]) else "Permit action",
        }
        if index.is_done("SCHD APC", doc_id):
            continue
        if args.no_download:
            row.update(status="indexed", fetched_at=now_iso())
            if ("SCHD APC", doc_id) not in index.seen:
                index.append(row)
            continue
        if budget["left"] <= 0:
            logger.warning("Download budget exhausted; remaining Shelby documents left for a later run")
            break
        stem = safe_stem(f"schd_{doc_id}_{d['slug'] or d.get('label', '')}")
        path, ct, nbytes, status = fetch_file(s, row["archive_url"], pdf_dir / stem, args.timeout * 3)
        row.update(pdf_path=str(path) if path else "", content_type=ct, bytes=str(nbytes),
                   status=status, fetched_at=now_iso())
        index.append(row)
        budget["left"] -= 1
        n_new += 1
        (logger.info if status == "ok" else logger.warning)(f"  {status}: {doc_id} {d['text'][:70]}")
        time.sleep(max(args.delay, 2.0))  # archive.org rate-limits aggressively
    logger.success(f"Shelby lane done: {n_new} new downloads this run")


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agencies", default="tdec,schd", help="Comma list of tdec,schd (default both)")
    ap.add_argument("--facility-ids", default="", help="TDEC facility ids to pull instead of matching (e.g. 63-0339,83-0526)")
    ap.add_argument("--no-discovery", action="store_true", help="Skip the eFiles keyword/alias sweeps")
    ap.add_argument("--include-types", default="", help="Only these eFiles file types (comma list)")
    ap.add_argument("--skip-types", default=",".join(DEFAULT_SKIP_TYPES), help="eFiles file types to skip")
    ap.add_argument("--shelby-source", choices=["auto", "live", "wayback", "skip"], default="auto")
    ap.add_argument("--shelby-all", action="store_true", help="Shelby: every archived permit-action doc, not just data centers")
    ap.add_argument("--limit", type=int, default=0, help="Max downloads this run (0 = no per-run limit)")
    ap.add_argument("--max-downloads", type=int, default=1000, help="Cap on total downloaded documents in the index")
    ap.add_argument("--no-download", action="store_true", help="Build the index only")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "pdfs").mkdir(exist_ok=True)
    logger.add(OUTPUT_DIR / "tn_dc_permits.log", level="INFO", rotation="5 MB")

    index = Index(OUTPUT_DIR / INDEX_NAME)
    left = max(0, args.max_downloads - index.n_ok)
    if args.limit:
        left = min(left, args.limit)
    budget = {"left": left}
    logger.info(f"Download budget this run: {left}")

    agencies = {a.strip().lower() for a in args.agencies.split(",") if a.strip()}
    if "tdec" in agencies:
        try:
            run_tdec(args, index, budget)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"TDEC lane failed: {e}")
    if "schd" in agencies:
        try:
            run_shelby(args, index, budget)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Shelby lane failed: {e}")
    logger.success(f"Index: {index.path} ({index.n_ok} documents on disk)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
