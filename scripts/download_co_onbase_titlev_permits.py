"""CDPHE OnBase (OBPA) stationary-source permit scraper.

Complements download_co_titlev_permits.py: the public Google Drive index only
covers ~239 companies, while CDPHE's Hyland OnBase Public Access portal
(oitco.hylandcloud.com/CDPHERMPublicAccess) exposes the full records system
through an unauthenticated JSON API:

    POST api/CustomQuery/KeywordSearch
        {"QueryID":"298","Keywords":[{"ID":"673","Value":"<AIRS>*"}],"QueryLimit":0}
        (QueryID 298 = "CDPHERM Air Stationary Source Permitting";
         keyword 673 = CDPHERM AIRS ID, 702 = Company Name)
    GET  api/Document/<url-encoded encrypted doc ID>  -> PDF

Targets are ECHO CO air majors; their AIRS IDs come from ECHO SourceID
(CO..08CCCPPPPP -> "CCC-PPPP" county-plant form used in OnBase doc names,
e.g. Suncor CO0000000800100003 -> 001-0003).

Doc names look like "001-0003_PRMT OP APPL_95OPAD108_6/4/2026"; the second
underscore token is the document type. The download stage keeps operating-
permit issuance docs (type contains OP, not APPL) by default; --doc-type-rx
overrides.

Stages (resumable):
    python scripts/download_co_onbase_titlev_permits.py enumerate
    python scripts/download_co_onbase_titlev_permits.py download [--doc-type-rx RX]

Output: data/raw/co_onbase_titlev_permits/
    co_onbase_docs_index.csv    every permitting doc found per target AIRS
    co_onbase_download_log.csv  per-document download status
    pdfs/                       <AIRS>_<sanitized doc name>.pdf
"""
import argparse
import csv
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

BASE = "https://oitco.hylandcloud.com/CDPHERMPublicAccess"
QUERY_ID = "298"
KW_AIRS = "673"
ECHO_CSV = RAW_DATA_DIR / "echo_air_majors" / "echo_air_majors.csv"
OUT_DIR = RAW_DATA_DIR / "co_onbase_titlev_permits"
DOC_INDEX = OUT_DIR / "co_onbase_docs_index.csv"
DL_LOG = OUT_DIR / "co_onbase_download_log.csv"
PDF_DIR = OUT_DIR / "pdfs"
DELAY = 0.5
DEFAULT_DOC_TYPE_RX = r"\bOP\b(?!.*APPL)|^PRMT OP(?!.*APPL)"


def airs_from_source_id(source_id: str):
    """CO0000000800100003 -> '001-0003' (county-plant).

    KNOWN GAP: ~106 ECHO CO majors (mostly Weld County O&G pads) have
    ALPHANUMERIC plant IDs (CO000000081230A05C -> county 123, plant 0A05C);
    digit-stripping mangles these and no OnBase AIRS format tested matches.
    Chase via Company Name keyword (ID 702) in a follow-up pass.
    """
    digits = re.sub(r"\D", "", source_id or "")
    if len(digits) < 8:
        return None
    tail = digits[-8:]
    county, plant = tail[:3], tail[3:]
    plant4 = plant.lstrip("0").rjust(4, "0") or "0000"
    return f"{county}-{plant4}"


def new_session():
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
    return s


def search_airs(s, airs):
    r = s.post(f"{BASE}/api/CustomQuery/KeywordSearch",
               json={"QueryID": QUERY_ID,
                     "Keywords": [{"ID": KW_AIRS, "Value": airs + "*"}],
                     "QueryLimit": 0},
               timeout=120)
    r.raise_for_status()
    j = r.json()
    return j.get("Data") or [], bool(j.get("Truncated"))


def doc_type(name: str) -> str:
    parts = name.split("_")
    return parts[1] if len(parts) > 1 else ""


def enumerate_cmd(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = {}
    for r in csv.DictReader(ECHO_CSV.open(encoding="utf-8")):
        if r["AIRState"] != "CO":
            continue
        airs = airs_from_source_id(r["SourceID"])
        if airs:
            targets.setdefault(airs, r["AIRName"])
    print(f"{len(targets)} target AIRS IDs from ECHO CO majors")
    s = new_session()
    seen_docs = set()
    if DOC_INDEX.exists():
        seen_airs = {r["airs"] for r in csv.DictReader(DOC_INDEX.open(encoding="utf-8"))}
    else:
        seen_airs = set()
    new_file = not DOC_INDEX.exists()
    with DOC_INDEX.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["airs", "echo_name", "doc_id", "doc_name",
                                          "doc_type", "truncated"])
        if new_file:
            w.writeheader()
        n_docs = 0
        for i, (airs, name) in enumerate(sorted(targets.items()), 1):
            if airs in seen_airs:
                continue
            try:
                docs, truncated = search_airs(s, airs)
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(targets)}] {airs}: ERROR {e}", flush=True)
                continue
            for d in docs:
                if d["ID"] in seen_docs:
                    continue
                seen_docs.add(d["ID"])
                w.writerow({"airs": airs, "echo_name": name, "doc_id": d["ID"],
                            "doc_name": d["Name"], "doc_type": doc_type(d["Name"]),
                            "truncated": truncated})
                n_docs += 1
            f.flush()
            print(f"[{i}/{len(targets)}] {airs} {name[:35]}: {len(docs)} docs"
                  f"{' TRUNCATED' if truncated else ''}", flush=True)
            time.sleep(DELAY)
    print(f"{DOC_INDEX}: {n_docs} new docs")


def download_cmd(args):
    rx = re.compile(args.doc_type_rx, re.I)
    docs = [d for d in csv.DictReader(DOC_INDEX.open(encoding="utf-8"))
            if rx.search(d["doc_type"])]
    done = set()
    if DL_LOG.exists():
        done = {r["doc_id"] for r in csv.DictReader(DL_LOG.open(encoding="utf-8"))
                if r["status"] == "downloaded"}
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{len(docs)} docs match doc-type filter, {len(done)} already downloaded")
    s = new_session()
    new_log = not DL_LOG.exists()
    n_ok = n_err = 0
    with DL_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["doc_id", "airs", "doc_name", "filename",
                                          "status", "size_bytes", "error"])
        if new_log:
            w.writeheader()
        for i, d in enumerate(docs, 1):
            if d["doc_id"] in done:
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", d["doc_name"])[:120].strip("_")
            fname = f"{d['airs']}_{safe}.pdf"
            dest = PDF_DIR / fname
            try:
                if not (dest.exists() and dest.stat().st_size > 0):
                    enc = urllib.parse.quote(d["doc_id"], safe="")
                    r = s.get(f"{BASE}/api/Document/{enc}", timeout=600)
                    r.raise_for_status()
                    if not r.content.startswith(b"%PDF"):
                        raise ValueError(f"not a PDF ({r.headers.get('Content-Type')})")
                    tmp = dest.with_suffix(".pdf.part")
                    tmp.write_bytes(r.content)
                    tmp.replace(dest)
                w.writerow({"doc_id": d["doc_id"], "airs": d["airs"],
                            "doc_name": d["doc_name"], "filename": fname,
                            "status": "downloaded",
                            "size_bytes": dest.stat().st_size, "error": ""})
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                w.writerow({"doc_id": d["doc_id"], "airs": d["airs"],
                            "doc_name": d["doc_name"], "filename": fname,
                            "status": "error", "size_bytes": 0,
                            "error": f"{type(e).__name__}: {e}"[:200]})
                n_err += 1
            f.flush()
            if i % 25 == 0:
                print(f"[{i}/{len(docs)}] ok={n_ok} err={n_err}", flush=True)
            time.sleep(DELAY)
    print(f"done: ok={n_ok} err={n_err} skipped={len(done)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enumerate")
    e.set_defaults(fn=enumerate_cmd)
    d = sub.add_parser("download")
    d.add_argument("--doc-type-rx", default=DEFAULT_DOC_TYPE_RX)
    d.set_defaults(fn=download_cmd)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
