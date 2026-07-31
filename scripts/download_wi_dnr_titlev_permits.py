"""Scrape Wisconsin DNR WARP for Part 70 (Title V) permit documents.

WARP (https://apps.dnr.wi.gov/warp_ext/) is classic ASP.NET WebForms — every
step is a __VIEWSTATE postback:

  1. AM_PermitTrackingSearch.aspx: POST __EVENTTARGET=...$btnSearch with
     chkPart70=on -> gvResult grid, ALL Part 70 facilities in one page
     (~335 rows, no pager).
  2. Each result row's lnkSelect postback 302-redirects to
     AM_PermitTracking2.aspx?id=<AM_FACILITY_SEQ_NO> — that's how FIDs map
     to facility seq ids.
  3. On the facility page, __doPostBack('...$gvPermits','Select$N') reveals
     the gvDocuments grid for that permit.
  4. __doPostBack('...$gvDocuments','Select$M') re-renders row M's download
     link as a DIRECT sessionless URL: AM_DownloadObject.aspx?id=<object_id>.
  5. GET that URL -> PDF (they have real text layers; no OCR needed).

Usage:
    python scripts/download_wi_dnr_titlev_permits.py enumerate
        [--types FOP,Con-OP] [--include-inactive] [--delay 0.4]
    python scripts/download_wi_dnr_titlev_permits.py download
        [--doc-regex final_permit] [--delay 0.4]

enumerate writes wi_dnr_part70_facilities.csv, wi_dnr_permits.csv and
wi_dnr_doc_index.csv (with object ids); download GETs docs whose name
matches --doc-regex into pdfs/. Both stages resume from their CSVs.
"""

import argparse
import csv
import html as htmllib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from loguru import logger

BASE = "https://apps.dnr.wi.gov/warp_ext/"
SEARCH_URL = BASE + "AM_PermitTrackingSearch.aspx"
FACILITY_URL = BASE + "AM_PermitTracking2.aspx?id={seq}"
DOWNLOAD_URL = BASE + "AM_DownloadObject.aspx?id={oid}"

OUT_DIR = Path("data/raw/wi_dnr_titlev_permits")
FACILITIES_CSV = OUT_DIR / "wi_dnr_part70_facilities.csv"
PERMITS_CSV = OUT_DIR / "wi_dnr_permits.csv"
DOC_INDEX_CSV = OUT_DIR / "wi_dnr_doc_index.csv"
DL_LOG_CSV = OUT_DIR / "wi_dnr_download_log.csv"
PDF_DIR = OUT_DIR / "pdfs"

P = "ctl00$ContentPlaceHolder1$"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_openers():
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    cp = urllib.request.HTTPCookieProcessor(cj)
    op = urllib.request.build_opener(cp)
    op_nr = urllib.request.build_opener(cp, NoRedirect())
    for o in (op, op_nr):
        o.addheaders = [("User-Agent", "Mozilla/5.0 (research; permit dataset)")]
    return op, op_nr


def serialize_form(html):
    """Collect the ASP.NET form state: hidden/text inputs, checked
    checkboxes, selected options."""
    form = {}
    for m in re.finditer(r"<input([^>]*)>", html):
        a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if "name" not in a:
            continue
        t = a.get("type", "text")
        if t in ("text", "hidden"):
            form[a["name"]] = a.get("value", "")
        elif t == "checkbox" and "checked" in m.group(1):
            form[a["name"]] = a.get("value", "on")
    for m in re.finditer(r'<select name="([^"]+)"[^>]*>(.*?)</select>', html, re.S):
        sel = re.search(r'<option selected[^>]*value="([^"]*)"', m.group(2))
        form[m.group(1)] = sel.group(1) if sel else ""
    return form


def request(op, url, form=None, timeout=120, retries=4):
    delay = 5
    for attempt in range(retries):
        try:
            data = urllib.parse.urlencode(form).encode() if form is not None else None
            return op.open(urllib.request.Request(url, data=data), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302):
                raise
            if attempt == retries - 1:
                raise
            logger.warning(f"HTTP {e.code} on {url[:80]}, retry in {delay}s")
        except Exception as e:
            if attempt == retries - 1:
                raise
            logger.warning(f"{e} on {url[:80]}, retry in {delay}s")
        time.sleep(delay)
        delay *= 2


def postback(op, url, form, target, argument=""):
    form = dict(form)
    form["__EVENTTARGET"] = target
    form["__EVENTARGUMENT"] = argument
    return request(op, url, form)


def grid_html(page, grid_id):
    """Extract a GridView's <table> block by element id."""
    m = re.search(r'id="%s".*?</table>' % re.escape(grid_id), page, re.S)
    return m.group(0) if m else ""


def clean(s):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def search_part70(op):
    """POST the Part 70 search; return (results_html, serialized_form)."""
    html = request(op, SEARCH_URL).read().decode("utf-8", "replace")
    form = serialize_form(html)
    form[P + "chkPart70"] = "on"
    form["__EVENTTARGET"] = P + "btnSearch"
    form["__EVENTARGUMENT"] = ""
    res = request(op, SEARCH_URL, form).read().decode("utf-8", "replace")
    # NOTE: the results page does NOT re-render the search controls, so the
    # row-select postback must NOT include chkPart70 etc. — ASP.NET event
    # validation 500s on unregistered fields.
    return res, serialize_form(res)


def parse_result_rows(res):
    rows = []
    for m in re.finditer(
            r'gvResult_(ctl\d+)_lblFid"[^>]*>([^<]*)</span>.*?'
            r'gvResult_\1_lblName"[^>]*>([^<]*)</span>.*?'
            r'gvResult_\1_lblAddress"[^>]*>([^<]*)</span>.*?'
            r'gvResult_\1_lblCity"[^>]*>([^<]*)</span>.*?'
            r'gvResult_\1_lblCounty"[^>]*>([^<]*)</span>', res, re.S):
        rows.append({
            "ctl": m.group(1), "fid": clean(m.group(2)),
            "facility_name": clean(m.group(3)), "address": clean(m.group(4)),
            "city": clean(m.group(5)), "county": clean(m.group(6)),
        })
    return rows


def enumerate_facilities(op, op_nr, delay):
    if FACILITIES_CSV.exists():
        with open(FACILITIES_CSV) as f:
            done = list(csv.DictReader(f))
        if done:
            logger.info(f"facilities: {len(done)} already enumerated, skipping")
            return done
    res, form = search_part70(op)
    rows = parse_result_rows(res)
    logger.info(f"Part 70 search returned {len(rows)} facilities")
    out = []
    for i, r in enumerate(rows):
        target = f"{P}gvResult${r['ctl']}$lnkSelect"
        try:
            request(op_nr, SEARCH_URL,
                    dict(form, __EVENTTARGET=target, __EVENTARGUMENT=""))
            logger.error(f"{r['fid']}: no redirect from row select")
            seq = ""
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            m = re.search(r"id=(\d+)", loc)
            seq = m.group(1) if m else ""
        out.append({**{k: v for k, v in r.items() if k != "ctl"},
                    "facility_seq": seq})
        if (i + 1) % 25 == 0:
            logger.info(f"resolved {i + 1}/{len(rows)} facility ids")
        time.sleep(delay)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(FACILITIES_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    logger.info(f"-> {FACILITIES_CSV} ({len(out)} rows)")
    return out


def parse_permit_rows(page):
    """Parse gvPermits rows: Select$N link + cells."""
    block = grid_html(page, "ctl00_ContentPlaceHolder1_gvPermits")
    permits = []
    for m in re.finditer(
            r"__doPostBack\(&#39;ctl00\$ContentPlaceHolder1\$gvPermits&#39;,"
            r"&#39;Select\$(\d+)&#39;\)[^>]*>Select</a>\s*</td>(.*?)</tr>",
            block, re.S):
        cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)]
        permits.append({"select_idx": m.group(1), "cells": cells})
    return permits


def parse_doc_rows(page):
    block = grid_html(page, "ctl00_ContentPlaceHolder1_gvDocuments")
    docs = []
    for m in re.finditer(
            r"__doPostBack\(&#39;ctl00\$ContentPlaceHolder1\$gvDocuments&#39;,"
            r"&#39;Select\$(\d+)&#39;\)[^>]*>Select</a>\s*</td>(.*?)</tr>",
            block, re.S):
        cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)]
        docs.append({"select_idx": m.group(1),
                     "permit_no": cells[-2] if len(cells) > 1 else "",
                     "doc_name": cells[-1] if cells else ""})
    return docs


def enumerate_docs(op, facilities, types, include_inactive, delay):
    seen_fac = set()
    if DOC_INDEX_CSV.exists():
        with open(DOC_INDEX_CSV) as f:
            for row in csv.DictReader(f):
                seen_fac.add(row["facility_seq"])
    perm_f = open(PERMITS_CSV, "a", newline="")
    perm_w = csv.writer(perm_f)
    if perm_f.tell() == 0:
        perm_w.writerow(["facility_seq", "fid", "facility_name", "permit_no",
                         "type", "app_received", "app_complete", "public_notice",
                         "decision", "status", "permit_writer"])
    doc_f = open(DOC_INDEX_CSV, "a", newline="")
    doc_w = csv.writer(doc_f)
    if doc_f.tell() == 0:
        doc_w.writerow(["facility_seq", "fid", "facility_name", "permit_no",
                        "permit_type", "doc_name", "object_id"])
    todo = [x for x in facilities if x["facility_seq"]
            and x["facility_seq"] not in seen_fac]
    logger.info(f"enumerating docs for {len(todo)} facilities "
                f"({len(seen_fac)} already done), types={types}")
    for i, fac in enumerate(todo):
        url = FACILITY_URL.format(seq=fac["facility_seq"])
        try:
            page = request(op, url).read().decode("utf-8", "replace")
            form = serialize_form(page)
            if include_inactive:
                chk = re.search(r'name="(ctl00\$ContentPlaceHolder1\$chk\w*Inactive\w*)"',
                                page)
                if chk:
                    form[chk.group(1)] = "on"
                    page = postback(op, url, form, chk.group(1)).read().decode(
                        "utf-8", "replace")
                    form = serialize_form(page) | {chk.group(1): "on"}
            permits = parse_permit_rows(page)
            n_docs = 0
            for pm in permits:
                cells = pm["cells"] + [""] * 8
                permit_no, ptype = cells[0], cells[1]
                perm_w.writerow([fac["facility_seq"], fac["fid"],
                                 fac["facility_name"], permit_no, ptype,
                                 *cells[2:8]])
                if types and ptype not in types:
                    continue
                p2 = postback(op, url, form, P + "gvPermits",
                              f"Select${pm['select_idx']}").read().decode(
                    "utf-8", "replace")
                form2 = serialize_form(p2)
                time.sleep(delay)
                for doc in parse_doc_rows(p2):
                    p3 = postback(op, url, form2, P + "gvDocuments",
                                  f"Select${doc['select_idx']}").read().decode(
                        "utf-8", "replace")
                    m = re.search(r"AM_DownloadObject\.aspx\?id=(\d+)", p3)
                    doc_w.writerow([fac["facility_seq"], fac["fid"],
                                    fac["facility_name"], doc["permit_no"],
                                    ptype, doc["doc_name"],
                                    m.group(1) if m else ""])
                    n_docs += 1
                    time.sleep(delay)
            doc_f.flush(); perm_f.flush()
            logger.info(f"[{i + 1}/{len(todo)}] {fac['facility_name']}: "
                        f"{len(permits)} permits, {n_docs} docs indexed")
        except Exception as e:
            logger.error(f"{fac['facility_name']} ({fac['facility_seq']}): {e}")
        time.sleep(delay)
    perm_f.close(); doc_f.close()


def sanitize(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:180]


def download(doc_regex, delay):
    rx = re.compile(doc_regex, re.I)
    with open(DOC_INDEX_CSV) as f:
        docs = [r for r in csv.DictReader(f) if r["object_id"]
                and rx.search(r["doc_name"])]
    done = set()
    if DL_LOG_CSV.exists():
        with open(DL_LOG_CSV) as f:
            done = {r["object_id"] for r in csv.DictReader(f)
                    if r["status"] == "downloaded"}
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    log_f = open(DL_LOG_CSV, "a", newline="")
    log_w = csv.writer(log_f)
    if log_f.tell() == 0:
        log_w.writerow(["object_id", "fid", "permit_no", "filename", "status",
                        "size_bytes", "error"])
    todo = [d for d in docs if d["object_id"] not in done]
    logger.info(f"{len(docs)} docs match /{doc_regex}/i, "
                f"{len(todo)} to download ({len(done)} done)")
    op, _ = make_openers()
    for i, d in enumerate(todo):
        name = d["doc_name"] or f"{d['fid']}_{d['permit_no']}.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        # object_id in the name: the same doc title recurs across permit
        # revisions (Part II/III attachments), and those are distinct files
        fname = sanitize(f"{d['fid']}_{d['object_id']}_{name}")
        dest = PDF_DIR / fname
        try:
            r = request(op, DOWNLOAD_URL.format(oid=d["object_id"]), timeout=600)
            data = r.read()
            if not data.startswith(b"%PDF"):
                raise ValueError(f"not a PDF ({len(data)} bytes, "
                                 f"{r.headers.get('Content-Type')})")
            dest.write_bytes(data)
            log_w.writerow([d["object_id"], d["fid"], d["permit_no"], fname,
                            "downloaded", len(data), ""])
            logger.info(f"[{i + 1}/{len(todo)}] {fname} ({len(data):,}B)")
        except Exception as e:
            log_w.writerow([d["object_id"], d["fid"], d["permit_no"], fname,
                            "error", 0, f"{type(e).__name__}: {e}"])
            logger.error(f"{fname}: {e}")
        log_f.flush()
        time.sleep(delay)
    log_f.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    en = sub.add_parser("enumerate")
    en.add_argument("--types", default="FOP,Con-OP",
                    help="comma-separated permit types to index docs for; "
                         "empty = all")
    en.add_argument("--include-inactive", action="store_true")
    en.add_argument("--delay", type=float, default=0.4)
    dl = sub.add_parser("download")
    dl.add_argument("--doc-regex", default=r"final_permit")
    dl.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.add(OUT_DIR / f"{args.cmd}.log", rotation="20 MB")
    if args.cmd == "enumerate":
        types = {t.strip() for t in args.types.split(",") if t.strip()}
        op, op_nr = make_openers()
        facilities = enumerate_facilities(op, op_nr, args.delay)
        enumerate_docs(op, facilities, types, args.include_inactive, args.delay)
    else:
        download(args.doc_regex, args.delay)


if __name__ == "__main__":
    main()
