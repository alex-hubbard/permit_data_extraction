"""BAAQMD Title V (Major Facility Review) permit scraper.

baaqmd.gov is Sitecore; every table on the Title V pages is a TableBlock
widget whose full contents are exposed by an unauthenticated CSV export:

    /admin/tableutils/CsvExportUnfiltered?pageId=<page guid>
        &dataSourceId=<table guid or "query:.">&renderingParamsHash=<hash>

TableBlock instantiations (pageId, elemId, dataSourceId, hash) are parsed
straight out of the page HTML. The main Title V Permits page holds one table
per county listing facilities with links to per-facility document pages; each
facility page holds one table of document links (current permit, statements
of basis, EPA letters, monitoring reports, ...).

Stages (resumable):
    python scripts/download_baaqmd_titlev_permits.py enumerate
    python scripts/download_baaqmd_titlev_permits.py download [--all-docs]

By default only documents whose label/filename mentions permit / statement of
basis are downloaded (skips semi-annual monitoring reports); --all-docs takes
everything.

Output: data/raw/baaqmd_titlev_permits/
    baaqmd_facilities_index.csv   site id, name, county, facility page URL
    baaqmd_docs_index.csv         per-facility document link inventory
    baaqmd_download_log.csv       per-document download status
    pdfs/                         <site>_<basename>.pdf
"""
import argparse
import csv
import html
import io
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import RAW_DATA_DIR  # noqa: E402

BASE = "https://www.baaqmd.gov"
LIST_PAGE = f"{BASE}/en/Permits/Major-Facility-Review-title-V/Title-V-Permits"
OUT_DIR = RAW_DATA_DIR / "baaqmd_titlev_permits"
FAC_INDEX = OUT_DIR / "baaqmd_facilities_index.csv"
DOC_INDEX = OUT_DIR / "baaqmd_docs_index.csv"
DL_LOG = OUT_DIR / "baaqmd_download_log.csv"
PDF_DIR = OUT_DIR / "pdfs"
DELAY = 0.5

TABLEBLOCK_RX = re.compile(
    r'TableBlock\("([0-9a-f-]{36})",\s*"(tbl[0-9a-f]{32})",\s*"([^"]+)",\s*(-?\d+)')
LINK_RX = re.compile(r'<a href="([^"]+)"[^>]*>([^<]*)</a>\s*(?:\(([^)]*)\))?')
PERMIT_DOC_RX = re.compile(r"permit|statement.of.basis|\bsob\b", re.I)
FAC_PAGE_RX = re.compile(r"/Title-V-Permits/Page-Resources/Table-Data/", re.I)


def new_session():
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
    return s


def table_csv(s, page_id, ds_id, rhash):
    r = s.get(f"{BASE}/admin/tableutils/CsvExportUnfiltered",
              params={"pageId": page_id, "dataSourceId": ds_id,
                      "renderingParamsHash": rhash}, timeout=120)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))


def page_tableblocks(s, url):
    r = s.get(url, timeout=120)
    r.raise_for_status()
    return TABLEBLOCK_RX.findall(r.text)


def enumerate_cmd(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = new_session()
    facilities = {}
    for page_id, _elem, ds_id, rhash in page_tableblocks(s, LIST_PAGE):
        rows = table_csv(s, page_id, ds_id, rhash)
        time.sleep(DELAY)
        for row in rows:
            docs = row.get("Documents") or ""
            m = LINK_RX.search(docs)
            if not m or not FAC_PAGE_RX.search(m.group(1)):
                continue
            site = (row.get("Site") or "").strip()
            url = urljoin(BASE, html.unescape(m.group(1)))
            facilities[site or url] = {
                "site": site, "name": m.group(2).strip(),
                "city": (row.get("City") or "").strip(), "page_url": url,
                "county": url.split("/Table-Data/")[1].split("/")[0],
            }
        print(f"table {ds_id[:20]}...: {len(rows)} rows "
              f"(facilities so far {len(facilities)})", flush=True)
    with FAC_INDEX.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["site", "name", "city", "county", "page_url"])
        w.writeheader()
        w.writerows(facilities.values())
    print(f"{FAC_INDEX}: {len(facilities)} facilities")

    # per-facility document inventory
    with DOC_INDEX.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["site", "name", "doc_label", "doc_note",
                                          "doc_url"])
        w.writeheader()
        n_docs = 0
        for i, fac in enumerate(facilities.values(), 1):
            try:
                blocks = page_tableblocks(s, fac["page_url"])
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(facilities)}] {fac['site']}: page error {e}",
                      flush=True)
                continue
            for page_id, _elem, ds_id, rhash in blocks:
                try:
                    rows = table_csv(s, page_id, ds_id, rhash)
                except Exception as e:  # noqa: BLE001
                    print(f"[{i}/{len(facilities)}] {fac['site']}: table error {e}",
                          flush=True)
                    continue
                for row in rows:
                    for m in LINK_RX.finditer(row.get("Docs") or ""):
                        url = html.unescape(m.group(1))
                        if ".pdf" not in url.lower():
                            continue
                        w.writerow({"site": fac["site"], "name": fac["name"],
                                    "doc_label": m.group(2).strip(),
                                    "doc_note": (m.group(3) or "").strip(),
                                    "doc_url": urljoin(BASE, url)})
                        n_docs += 1
            f.flush()
            print(f"[{i}/{len(facilities)}] {fac['site']} {fac['name'][:35]}: "
                  f"docs so far {n_docs}", flush=True)
            time.sleep(DELAY)
    print(f"{DOC_INDEX}: {n_docs} document links")


def download_cmd(args):
    docs = list(csv.DictReader(DOC_INDEX.open(encoding="utf-8")))
    if not args.all_docs:
        # label only — every doc_url contains "title-v-permits", so matching
        # the URL would keep monitoring reports and letters too
        docs = [d for d in docs if PERMIT_DOC_RX.search(d["doc_label"])]
    done = set()
    if DL_LOG.exists():
        done = {r["doc_url"] for r in csv.DictReader(DL_LOG.open(encoding="utf-8"))
                if r["status"] == "downloaded"}
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{len(docs)} docs to download "
          f"({'all' if args.all_docs else 'permit/SOB only'}), {len(done)} done")
    s = new_session()
    new_log = not DL_LOG.exists()
    n_ok = n_err = 0
    with DL_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["site", "doc_label", "doc_url", "filename",
                                          "status", "size_bytes", "error"])
        if new_log:
            w.writeheader()
        for i, d in enumerate(docs, 1):
            if d["doc_url"] in done:
                continue
            base = re.sub(r"[?#].*$", "", d["doc_url"]).rsplit("/", 1)[-1]
            # site ids can contain '/' (e.g. "A1438/E0459") — keep paths flat
            fname = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{d['site']}_{base}")
            dest = PDF_DIR / fname
            try:
                if not (dest.exists() and dest.stat().st_size > 0):
                    r = s.get(d["doc_url"], timeout=600)
                    r.raise_for_status()
                    if not r.content.startswith(b"%PDF"):
                        raise ValueError(f"not a PDF ({r.headers.get('Content-Type')})")
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    tmp.write_bytes(r.content)
                    tmp.replace(dest)
                w.writerow({"site": d["site"], "doc_label": d["doc_label"],
                            "doc_url": d["doc_url"], "filename": fname,
                            "status": "downloaded",
                            "size_bytes": dest.stat().st_size, "error": ""})
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                w.writerow({"site": d["site"], "doc_label": d["doc_label"],
                            "doc_url": d["doc_url"], "filename": fname,
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
    d.add_argument("--all-docs", action="store_true")
    d.set_defaults(fn=download_cmd)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
