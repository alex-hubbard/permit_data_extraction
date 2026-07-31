"""Download Colorado CDPHE Title V operating permits from the public Google
Drive company index.

CDPHE publishes every current Title V operating permit (plus Technical Review
documents) in five public Drive folders linked from
https://cdphe.colorado.gov/apcd/titlev#index (A-E, F-J, K-P, Q-V, W-Z), one
subfolder per company/facility. No auth: folders are enumerated via the public
`embeddedfolderview` HTML endpoint and files fetched via
drive.usercontent.google.com (handles the large-file confirm page).

Resumable: files already on disk (non-empty) are skipped; every attempt is
appended to co_titlev_download_log.csv. Enumeration is re-run each invocation
(it is cheap - a few hundred folder listings).

Usage:
    python scripts/download_co_titlev_permits.py [--out data/raw/co_titlev_permits]
        [--delay 1.0] [--enumerate-only]
"""

import argparse
import csv
import html
import re
import time
from pathlib import Path

import requests
from loguru import logger

LETTER_FOLDERS = {
    "A-E": "0B0tmPQ67k3NVUXY0b0pmaGlCS3M",
    "F-J": "0B0tmPQ67k3NVX1pxTFlHRklYQUE",
    "K-P": "0B0tmPQ67k3NVSGpkZ0ktbzZWY0k",
    "Q-V": "0B0tmPQ67k3NVQWxxdExYaUp5eVU",
    "W-Z": "0B0tmPQ67k3NVbTZMY1lVSm51SlE",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
FOLDER_VIEW = "https://drive.google.com/embeddedfolderview?id={fid}{rk}#list"
FILE_DL = "https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t{rk}"

ENTRY_RE = re.compile(
    r'<div class="flip-entry"[^>]*>.*?href="([^"]+)".*?flip-entry-title">([^<]+)',
    re.S)
FOLDER_CAP = 1000  # embeddedfolderview shows at most ~1000 entries


def list_folder(sess, fid, resourcekey=""):
    rk = f"&resourcekey={resourcekey}" if resourcekey else ""
    r = sess.get(FOLDER_VIEW.format(fid=fid, rk=rk), timeout=60)
    r.raise_for_status()
    entries = []
    for href, title in ENTRY_RE.findall(r.text):
        href = html.unescape(href)
        title = html.unescape(title).strip()
        m = re.search(r"/folders/([\w-]+)(?:\?resourcekey=([\w-]*))?", href)
        if m:
            entries.append(("folder", m.group(1), m.group(2) or "", title))
            continue
        m = re.search(r"/file/d/([\w-]+)", href)
        if m:
            rk2 = re.search(r"resourcekey=([\w-]+)", href)
            entries.append(("file", m.group(1), rk2.group(1) if rk2 else "", title))
    if len(entries) >= FOLDER_CAP:
        logger.warning(f"folder {fid} returned {len(entries)} entries - may be capped")
    return entries


def safe_name(s, maxlen=140):
    s = re.sub(r"[^\w\-. ]", "_", s).strip()
    return re.sub(r"\s+", " ", s)[:maxlen]


def enumerate_all(sess):
    """Walk letter folders -> company folders (-> nested folders) -> files."""
    rows = []
    for letter, fid in LETTER_FOLDERS.items():
        companies = list_folder(sess, fid)
        logger.info(f"[{letter}] {len(companies)} entries")
        for kind, cfid, crk, cname in companies:
            if kind != "folder":
                rows.append((letter, "(root)", cfid, crk, cname))
                continue
            stack = [(cfid, crk, "")]
            while stack:
                sfid, srk, prefix = stack.pop()
                for k2, f2, rk2, t2 in list_folder(sess, sfid, srk):
                    if k2 == "folder":
                        stack.append((f2, rk2, f"{prefix}{safe_name(t2)}__"))
                    else:
                        rows.append((letter, cname, f2, rk2, prefix + t2))
            time.sleep(0.2)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/raw/co_titlev_permits")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--enumerate-only", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    pdf_dir = out / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers["User-Agent"] = UA

    rows = enumerate_all(sess)
    index_path = out / "co_titlev_index.csv"
    with open(index_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["letter", "company", "file_id", "resourcekey", "file_name"])
        w.writerows(rows)
    logger.info(f"enumerated {len(rows)} files across "
                f"{len({r[1] for r in rows})} companies -> {index_path}")
    if args.enumerate_only:
        return

    log_path = out / "co_titlev_download_log.csv"
    new_log = not log_path.exists()
    ok = err = skipped = 0
    with open(log_path, "a", newline="", encoding="utf-8") as logf:
        w = csv.writer(logf)
        if new_log:
            w.writerow(["file_id", "local_name", "status", "bytes", "error"])
        for letter, company, fid, rk, fname in rows:
            local = pdf_dir / f"{safe_name(company)}__{safe_name(fname)}"
            if not local.suffix:
                local = local.with_suffix(".pdf")
            if local.exists() and local.stat().st_size > 0:
                skipped += 1
                continue
            try:
                url = FILE_DL.format(fid=fid, rk=f"&resourcekey={rk}" if rk else "")
                r = sess.get(url, timeout=300)
                r.raise_for_status()
                if r.content[:15].lstrip().startswith(b"<"):
                    raise RuntimeError(f"HTML response ({len(r.content)} B) - not a file")
                local.write_bytes(r.content)
                ok += 1
                w.writerow([fid, local.name, "ok", len(r.content), ""])
                logger.info(f"[{ok+err+skipped}/{len(rows)}] {local.name} ({len(r.content)//1024} KB)")
            except Exception as e:
                err += 1
                w.writerow([fid, local.name, "error", 0, str(e)[:200]])
                logger.warning(f"{fname}: {e}")
            logf.flush()
            time.sleep(args.delay)
    logger.info(f"done: ok={ok} err={err} skipped={skipped}")


if __name__ == "__main__":
    main()
