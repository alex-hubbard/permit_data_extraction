#!/usr/bin/env python3
"""Enumerate Maricopa County AQD data-center facilities (Phoenix metro).

Stage 1 of the Maricopa scraper: the county's ArcGIS REST facility registry
(gis.maricopa.gov/arcgis/rest/services/AQD/ImpactFacility/MapServer/0) is
openly queryable and carries an explicit "Data Centers (>100 MW)" facility
type, plus data centers filed under "Data/Switch/Fulfillment" and
"Generator, Stationary (ATO)" types. This script pulls:

  1. every facility typed 'Data Centers%', and
  2. facilities matching dc_targets operator terms or "data center" in the
     facility/company name (any type),

writing a combined facility index (facility_id, names, address, type).

Stage 2 (follow-up): permit documents live in the IMPACT portal
(dm.maricopa.gov, Oracle ADF/JSF) keyed by facility_id — needs a Selenium
driver; facility_ids from this index are the entry points.

Output: <RAW_DATA_DIR>/az_maricopa_dc_facilities/az_maricopa_dc_facilities.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Dict, List

import requests
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import RAW_DATA_DIR

import dc_targets

LAYER = "https://gis.maricopa.gov/arcgis/rest/services/AQD/ImpactFacility/MapServer/0/query"
OUTPUT_DIR = RAW_DATA_DIR / "az_maricopa_dc_facilities"

FIELDS = [
    "facility_id", "facility_nm", "name", "address1", "address2", "city",
    "zip5", "facility_type_cd", "facility_type_dsc",
]


def query(where: str, timeout: int = 60) -> List[Dict]:
    # Layer does not support resultOffset pagination; window on OBJECTID.
    out, last_oid = [], 0
    while True:
        params = {
            "where": f"({where}) AND OBJECTID > {last_oid}",
            "outFields": "OBJECTID," + ",".join(FIELDS),
            "orderByFields": "OBJECTID",
            "returnGeometry": "false",
            "f": "json",
        }
        r = requests.get(LAYER, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"ArcGIS error for {where!r}: {data['error'].get('message')}")
        feats = data.get("features", [])
        out.extend(f["attributes"] for f in feats)
        if not data.get("exceededTransferLimit") or not feats:
            break
        last_oid = feats[-1]["attributes"]["OBJECTID"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match-column", default="both", choices=["facility_nm", "name", "both"])
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rows: Dict[str, Dict] = {}

    typed = query("facility_type_dsc LIKE 'Data Centers%'")
    for r in typed:
        r["match_reason"] = "type:Data Centers (>100 MW)"
        rows[r["facility_id"]] = r
    logger.info(f"{len(typed)} facilities typed 'Data Centers (>100 MW)'")

    named = query(
        "UPPER(facility_nm) LIKE '%DATA CENTER%' OR UPPER(facility_nm) LIKE '%DATACENTER%'"
        " OR UPPER(name) LIKE '%DATA CENTER%'"
    )
    for r in named:
        rows.setdefault(r["facility_id"], {**r, "match_reason": "name:data center"})
    logger.info(f"{len(named)} facilities with 'data center' in name")

    terms = [t for t in dc_targets.entity_terms("AZ") if len(t) >= 5]
    for term in terms:
        esc = term.upper().replace("'", "''")
        clauses = []
        if args.match_column in ("facility_nm", "both"):
            clauses.append(f"UPPER(facility_nm) LIKE '{esc}%'")
        if args.match_column in ("name", "both"):
            clauses.append(f"UPPER(name) LIKE '{esc}%'")
        try:
            hits = query(" OR ".join(clauses))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{term!r}: {e}")
            continue
        for r in hits:
            rows.setdefault(r["facility_id"], {**r, "match_reason": f"operator:{term}"})

    logger.success(f"{len(rows)} unique candidate facilities")

    out_path = OUTPUT_DIR / "az_maricopa_dc_facilities.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS + ["match_reason"], extrasaction="ignore")
        w.writeheader()
        for r in rows.values():
            w.writerow(r)
    logger.success(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
