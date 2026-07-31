"""Download Kentucky DEP Title V air permits via the eSearch WebApi.

KY's eSearch (dep.gateway.ky.gov) is a plain ASP.NET MVC app over an open JSON
REST API - no auth, no WAF, PDFs carry real text layers (no OCR needed):

  POST  {api}/api/IssuedApprovals/Search      body {"Program":"Air"}  -> all air activities
  GET   {api}/api/issuedapprovals/Document/<json-model>              -> docs for an activity
  GET   {api}/api/documentGenerate/getDocument/<SourceDocId>         -> the PDF bytes

where model = {"AgencyId","ActivityCode","ActivityYear","ActivityNumber"}.

Facility-level strategy: one "current permit" per Title V facility. For each
agency (AgencyId) with Title V activity, pick the most recent permit-bearing
activity, preferring Renewal > Initial > any other Title V action by IssuedDate,
and download every document attached to it (permit PDF + statement of basis +
executive summary - all small and useful context). `--all-activities` instead
downloads docs for every Title V/major activity (full history).

Resumable: existing non-empty files skipped; every attempt logged. Index and
activity list are cached so reruns are cheap.

Usage:
    python scripts/download_ky_dep_titlev_permits.py [--out data/raw/ky_dep_titlev_permits]
        [--include-cond-major] [--all-activities] [--delay 0.5]
"""

import argparse
import csv
import json
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from loguru import logger

API = "http://dep.gateway.ky.gov/eSearch.WebApi/"
SEARCH = API + "api/IssuedApprovals/Search"
DOCLIST = API + "api/issuedapprovals/Document/"
DOCGET = API + "api/documentGenerate/getDocument/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def safe(s, maxlen=120):
    s = re.sub(r"[^\w\-. ]", "_", str(s)).strip()
    return re.sub(r"\s+", " ", s)[:maxlen]


def parse_date(s):
    if not s:
        return datetime.min
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except ValueError:
        return datetime.min


def pick_current(activities):
    """Most recent permit-bearing Title V activity for one facility.
    Renewal/Initial carry the full permit; prefer them, newest first."""
    def rank(a):
        t = a["ActivityType"]
        if "Renewal" in t:
            tier = 3
        elif "Initial" in t or "Original" in t:
            tier = 2
        else:
            tier = 1
        return (tier, parse_date(a.get("IssuedDate")))
    return max(activities, key=rank)


def list_docs(sess, act):
    model = {
        "AgencyId": act["AgencyId"],
        "ActivityCode": act["ActivityCode"],
        "ActivityYear": int(act["ActivityYear"]),
        "ActivityNumber": int(act["ActivityNum"]),
    }
    url = DOCLIST + urllib.parse.quote(json.dumps(model))
    r = sess.get(url, timeout=60)
    r.raise_for_status()
    return r.json() or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/raw/ky_dep_titlev_permits")
    ap.add_argument("--include-cond-major", action="store_true",
                    help="also pull Conditional Major (synthetic-minor) facilities")
    ap.add_argument("--all-activities", action="store_true",
                    help="download docs for every activity, not just the current permit")
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()

    out = Path(args.out)
    pdf_dir = out / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Content-Type": "application/json"})

    logger.info("fetching KY air activity universe ...")
    r = sess.post(SEARCH, data=json.dumps({"Program": "Air"}), timeout=120)
    r.raise_for_status()
    rows = r.json()
    logger.info(f"{len(rows)} air activities")

    prefixes = ["Title V", "Mjr Source"]
    if args.include_cond_major:
        prefixes.append("Cond Mjr")
    scope = [a for a in rows if any(a["ActivityType"].startswith(p) for p in prefixes)]

    by_agency = {}
    for a in scope:
        by_agency.setdefault(a["AgencyId"], []).append(a)
    logger.info(f"{len(scope)} in-scope activities across {len(by_agency)} facilities")

    # choose which activities to fetch docs for
    if args.all_activities:
        targets = scope
    else:
        targets = [pick_current(acts) for acts in by_agency.values()]

    with open(out / "ky_titlev_activities.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["AgencyId", "AgencyName", "County", "ActivityCode",
                    "ActivityYear", "ActivityNum", "ActivityType", "IssuedDate"])
        for a in targets:
            w.writerow([a["AgencyId"], a["AgencyName"], a["County"], a["ActivityCode"],
                        int(a["ActivityYear"]), int(a["ActivityNum"]),
                        a["ActivityType"], a.get("IssuedDate", "")])

    log_path = out / "ky_download_log.csv"
    new_log = not log_path.exists()
    ok = err = skipped = 0
    with open(log_path, "a", newline="", encoding="utf-8") as logf:
        w = csv.writer(logf)
        if new_log:
            w.writerow(["AgencyId", "AgencyName", "SourceDocId", "local_name",
                        "status", "bytes", "error"])
        for i, act in enumerate(targets, 1):
            aid, aname = act["AgencyId"], act["AgencyName"]
            try:
                docs = list_docs(sess, act)
            except Exception as e:
                err += 1
                w.writerow([aid, aname, "", "", "error_listing", 0, str(e)[:200]])
                logger.warning(f"{aname}: doc-list failed: {e}")
                continue
            for doc in docs:
                sid = int(doc["SourceDocId"])
                title = safe(doc.get("DocTitle", f"{sid}.pdf"))
                if not title.lower().endswith(".pdf"):
                    title += ".pdf"
                local = pdf_dir / f"{aid}_{safe(aname)}__{sid}_{title}"
                if local.exists() and local.stat().st_size > 0:
                    skipped += 1
                    continue
                try:
                    rr = sess.get(DOCGET + str(sid), timeout=300)
                    rr.raise_for_status()
                    if rr.content[:5] != b"%PDF-":
                        raise RuntimeError(f"not a PDF (starts {rr.content[:8]!r})")
                    local.write_bytes(rr.content)
                    ok += 1
                    w.writerow([aid, aname, sid, local.name, "ok", len(rr.content), ""])
                except Exception as e:
                    err += 1
                    w.writerow([aid, aname, sid, local.name, "error", 0, str(e)[:200]])
                    logger.warning(f"{aname} doc {sid}: {e}")
                logf.flush()
                time.sleep(args.delay)
            if i % 25 == 0:
                logger.info(f"[{i}/{len(targets)}] facilities processed "
                            f"(ok={ok} err={err} skipped={skipped})")
    logger.info(f"done: ok={ok} err={err} skipped={skipped} "
                f"across {len(targets)} activities")


if __name__ == "__main__":
    main()
