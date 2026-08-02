"""Oklahoma DEQ Permits-for-Public-Review scraper.

applications.deq.ok.gov/PermitsPublicReview is a plain ASP.NET app backed by
OnBase. The listing page renders every permit currently in the review pipeline
(including ones whose step is "Permit Issued") as a static HTML table; each row
links to permitinformation.aspx?id=<id>, whose document links are
javascript:window.location.href='download.ashx?docid=<doc>&cs=<checksum>'.

    GET viewpermits.aspx                     -> rows (division, county, company,
                                                facility, permit #, tier, type,
                                                step, staff, detail id)
    GET permitinformation.aspx?id=<id>       -> document list (label + docid/cs)
    GET download.ashx?docid=&cs=             -> the PDF

NOTE ON COVERAGE: this app exposes permits in the *current* review pipeline,
not OK's full issued-permit archive (DEQ keeps those "available for public
review at our office"). Expect a partial slice of Oklahoma's ~244 majors —
worth having since OK is otherwise at 0%, but not a complete state.

Stages (resumable):
    python scripts/download_ok_deq_titlev_permits.py enumerate
    python scripts/download_ok_deq_titlev_permits.py download [--all-types]

Output: data/raw/ok_deq_titlev_permits/
    ok_permits_index.csv    one row per permit in the pipeline
    ok_docs_index.csv       per-permit document links
    ok_download_log.csv     per-document download status
    pdfs/                   <permit#>_<docid>_<label>.pdf
"""
import argparse
import csv
import re
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

BASE = "https://applications.deq.ok.gov/PermitsPublicReview"
OUT_DIR = RAW_DATA_DIR / "ok_deq_titlev_permits"
PERMIT_INDEX = OUT_DIR / "ok_permits_index.csv"
DOC_INDEX = OUT_DIR / "ok_docs_index.csv"
DL_LOG = OUT_DIR / "ok_download_log.csv"
PDF_DIR = OUT_DIR / "pdfs"
DELAY = 0.4

ROW_RX = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL_RX = re.compile(r"<td>(.*?)</td>", re.S)
DETAIL_RX = re.compile(r"permitinformation\.aspx\?id=(\d+)")
DOC_RX = re.compile(r"download\.ashx\?docid=(\d+)&amp;cs=(\w+)'[^>]*>\s*([^<]*)")
TITLEV_RX = re.compile(r"title\s*v|part\s*70", re.I)
PERMIT_DOC_RX = re.compile(r"permit|statement of basis", re.I)


def text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


def new_session():
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
    return s


def enumerate_cmd(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = new_session()
    html = s.get(f"{BASE}/viewpermits.aspx", timeout=180).text
    permits = []
    for raw in ROW_RX.findall(html):
        cells = CELL_RX.findall(raw)
        if len(cells) < 10:
            continue
        vals = [text(c) for c in cells]
        m = DETAIL_RX.search(raw)
        if not m:
            continue
        permits.append({
            "detail_id": m.group(1), "division": vals[0], "county": vals[1],
            "company": vals[2], "facility": vals[3], "permit_no": vals[4],
            "tier": vals[5], "permit_type": vals[6], "step": vals[7],
            "status": vals[9] if len(vals) > 9 else "",
        })
    # one row per detail id
    uniq = {p["detail_id"]: p for p in permits}
    with PERMIT_INDEX.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(next(iter(uniq.values())).keys()))
        w.writeheader()
        w.writerows(uniq.values())
    tv = [p for p in uniq.values() if TITLEV_RX.search(p["permit_type"])]
    print(f"{PERMIT_INDEX}: {len(uniq)} permits ({len(tv)} Title V), "
          f"{len({p['company'] for p in tv})} distinct Title V companies")

    with DOC_INDEX.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["detail_id", "permit_no", "company",
                                          "facility", "permit_type", "doc_id",
                                          "checksum", "doc_label"])
        w.writeheader()
        n = 0
        targets = list(uniq.values()) if args.all_types else tv
        for i, p in enumerate(targets, 1):
            try:
                page = s.get(f"{BASE}/permitinformation.aspx",
                             params={"id": p["detail_id"]}, timeout=180).text
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(targets)}] {p['permit_no']}: ERROR {e}", flush=True)
                continue
            docs = DOC_RX.findall(page)
            for doc_id, cs, label in docs:
                w.writerow({"detail_id": p["detail_id"], "permit_no": p["permit_no"],
                            "company": p["company"], "facility": p["facility"],
                            "permit_type": p["permit_type"], "doc_id": doc_id,
                            "checksum": cs, "doc_label": text(label)})
                n += 1
            f.flush()
            print(f"[{i}/{len(targets)}] {p['permit_no']} {p['facility'][:32]}: "
                  f"{len(docs)} docs (total {n})", flush=True)
            time.sleep(DELAY)
    print(f"{DOC_INDEX}: {n} document links")


def download_cmd(args):
    docs = list(csv.DictReader(DOC_INDEX.open(encoding="utf-8")))
    if not args.all_docs:
        docs = [d for d in docs if PERMIT_DOC_RX.search(d["doc_label"])]
    done = set()
    if DL_LOG.exists():
        done = {r["doc_id"] for r in csv.DictReader(DL_LOG.open(encoding="utf-8"))
                if r["status"] == "downloaded"}
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{len(docs)} docs to download, {len(done)} already done")
    s = new_session()
    new_log = not DL_LOG.exists()
    n_ok = n_err = 0
    with DL_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["doc_id", "permit_no", "doc_label",
                                          "filename", "status", "size_bytes", "error"])
        if new_log:
            w.writeheader()
        for i, d in enumerate(docs, 1):
            if d["doc_id"] in done:
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_",
                          f"{d['permit_no']}_{d['doc_id']}_{d['doc_label']}")[:120]
            dest = PDF_DIR / (safe + ".pdf")
            try:
                if not (dest.exists() and dest.stat().st_size > 0):
                    r = s.get(f"{BASE}/download.ashx",
                              params={"docid": d["doc_id"], "cs": d["checksum"]},
                              timeout=900)
                    r.raise_for_status()
                    if not r.content.startswith(b"%PDF"):
                        raise ValueError(f"not a PDF ({r.headers.get('Content-Type')})")
                    tmp = dest.with_suffix(".pdf.part")
                    tmp.write_bytes(r.content)
                    tmp.replace(dest)
                w.writerow({"doc_id": d["doc_id"], "permit_no": d["permit_no"],
                            "doc_label": d["doc_label"], "filename": dest.name,
                            "status": "downloaded",
                            "size_bytes": dest.stat().st_size, "error": ""})
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                w.writerow({"doc_id": d["doc_id"], "permit_no": d["permit_no"],
                            "doc_label": d["doc_label"], "filename": dest.name,
                            "status": "error", "size_bytes": 0,
                            "error": f"{type(e).__name__}: {e}"[:200]})
                n_err += 1
            f.flush()
            if i % 20 == 0:
                print(f"[{i}/{len(docs)}] ok={n_ok} err={n_err}", flush=True)
            time.sleep(DELAY)
    print(f"done: ok={n_ok} err={n_err} skipped={len(done)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enumerate")
    e.add_argument("--all-types", action="store_true",
                   help="inventory docs for every permit type, not just Title V")
    e.set_defaults(fn=enumerate_cmd)
    d = sub.add_parser("download")
    d.add_argument("--all-docs", action="store_true")
    d.set_defaults(fn=download_cmd)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
