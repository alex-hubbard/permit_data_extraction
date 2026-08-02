"""SJVAPCD Public Permits portal scraper (apps.valleyair.org/PublicPermits).

The portal is a server-rendered ASP.NET app over server-side DataTables JSON
endpoints — no auth, no antiforgery token. Facility rows carry an isTitleV
flag, and document download is a two-step session dance:

    POST /Search/SearchByFacilityDataTable   (search form + DataTables paging)
    POST /Search/DownloadMultipleDocuments   {selectedFacilityIDs[]: <id>}
    GET  /Permit/GetDocumentFromFileForDownload   -> zip of the facility's
         current permit PDFs (real text layers, one PDF per permit section)

Empty searches 500 — enumeration sweeps the district's eight counties
(county-only advanced search is accepted). Facility IDs are region-prefixed
(N-/C-/S-).

Stages (both resumable):
    python scripts/download_sjvapcd_titlev_permits.py enumerate
    python scripts/download_sjvapcd_titlev_permits.py download [--all]

Output: data/raw/sjvapcd_titlev_permits/
    sjvapcd_facilities_index.csv   all facilities from the county sweep
    sjvapcd_download_log.csv       per-facility download status
    pdfs/                          <facilityID>_<zip member name>.pdf
"""
import argparse
import csv
import io
import sys
import time
import zipfile
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

BASE = "https://apps.valleyair.org/PublicPermits"
OUT_DIR = RAW_DATA_DIR / "sjvapcd_titlev_permits"
INDEX = OUT_DIR / "sjvapcd_facilities_index.csv"
DL_LOG = OUT_DIR / "sjvapcd_download_log.csv"
PDF_DIR = OUT_DIR / "pdfs"

COUNTIES = ["FRESNO", "KERN", "KINGS", "MADERA", "MERCED",
            "SAN JOAQUIN", "STANISLAUS", "TULARE"]
PAGE = 500
DELAY = 0.5
INDEX_FIELDS = ["id", "name", "street", "city", "zipCode", "county", "sicCode",
                "note", "numberOfPermits", "isTitleV"]


def form_body(**overrides):
    base = {
        "FacilityResultCount": "0",
        "PermitResultCount": "0",
        "Facility.Name": "", "Facility.SICCode": "", "Facility.Note": "",
        "Facility.Street": "", "Facility.City": "", "Facility.ZipCode": "",
        "Facility.County": "",
        "Permit.PermitID": "", "Permit.EquipmentDescription": "",
        "Permit.ApplicationStatus": "",
    }
    base.update(overrides)
    return base


def new_session():
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
    s.get(BASE, timeout=60)  # establish session cookies
    return s


def enumerate_cmd(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = new_session()
    seen = {}
    for county in COUNTIES:
        start, draw = 0, 1
        while True:
            body = form_body(**{"Facility.County": county})
            body.update({"draw": str(draw), "start": str(start), "length": str(PAGE)})
            r = s.post(f"{BASE}/Search/SearchByFacilityDataTable", data=body, timeout=120)
            r.raise_for_status()
            rows = r.json().get("data") or []
            for row in rows:
                seen[row["id"]] = {f: row.get(f) for f in INDEX_FIELDS}
            print(f"{county}: +{len(rows)} at offset {start} "
                  f"(total {len(seen)})", flush=True)
            if len(rows) < PAGE:
                break
            start += PAGE
            draw += 1
            time.sleep(DELAY)
        time.sleep(DELAY)
    with INDEX.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        w.writeheader()
        w.writerows(seen.values())
    n_tv = sum(1 for r in seen.values() if r["isTitleV"])
    print(f"{INDEX}: {len(seen)} facilities, {n_tv} Title V")


def download_cmd(args):
    facilities = list(csv.DictReader(INDEX.open(encoding="utf-8")))
    if not args.all:
        # The portal's isTitleV flag is never populated in search results, so
        # the Title V subset comes from ECHO-majors name matching (see
        # sjvapcd_titlev_target_ids.json, built by fuzzy-matching the index
        # against ECHO CA majors in the district's counties).
        import json
        targets = set(json.load(open(OUT_DIR / "sjvapcd_titlev_target_ids.json")))
        facilities = [r for r in facilities if r["id"] in targets]
    print(f"{len(facilities)} facilities to download "
          f"({'all' if args.all else 'ECHO-matched Title V targets'})")
    done = set()
    if DL_LOG.exists():
        done = {r["facility_id"] for r in csv.DictReader(DL_LOG.open(encoding="utf-8"))
                if r["status"] == "downloaded"}
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    s = new_session()
    new_log = not DL_LOG.exists()
    n_ok = n_err = 0
    with DL_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["facility_id", "name", "status",
                                          "n_pdfs", "bytes", "error"])
        if new_log:
            w.writeheader()
        for i, fac in enumerate(facilities, 1):
            fid = fac["id"]
            if fid in done:
                continue
            try:
                r = s.post(f"{BASE}/Search/DownloadMultipleDocuments",
                           data={"selectedFacilityIDs[]": fid}, timeout=300)
                r.raise_for_status()
                r = s.get(f"{BASE}/Permit/GetDocumentFromFileForDownload", timeout=600)
                r.raise_for_status()
                z = zipfile.ZipFile(io.BytesIO(r.content))
                n = 0
                for name in z.namelist():
                    if not name.lower().endswith(".pdf"):
                        continue
                    dest = PDF_DIR / f"{fid}_{Path(name).name}"
                    tmp = dest.with_suffix(".pdf.part")
                    tmp.write_bytes(z.read(name))
                    tmp.replace(dest)
                    n += 1
                w.writerow({"facility_id": fid, "name": fac["name"],
                            "status": "downloaded", "n_pdfs": n,
                            "bytes": len(r.content), "error": ""})
                n_ok += 1
                print(f"[{i}/{len(facilities)}] {fid} {fac['name'][:40]}: "
                      f"{n} PDFs ({len(r.content)/1e6:.1f}MB)", flush=True)
            except Exception as e:  # noqa: BLE001 — logged, resumable
                w.writerow({"facility_id": fid, "name": fac["name"],
                            "status": "error", "n_pdfs": 0, "bytes": 0,
                            "error": f"{type(e).__name__}: {e}"[:200]})
                n_err += 1
                print(f"[{i}/{len(facilities)}] {fid}: ERROR {e}", flush=True)
                s = new_session()  # download state is per-session; reset after failure
            f.flush()
            time.sleep(DELAY)
    print(f"done: ok={n_ok} err={n_err} skipped={len(done)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enumerate")
    e.set_defaults(fn=enumerate_cmd)
    d = sub.add_parser("download")
    d.add_argument("--all", action="store_true",
                   help="download every facility, not just Title V")
    d.set_defaults(fn=download_cmd)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
