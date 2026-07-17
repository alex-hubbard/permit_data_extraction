#!/usr/bin/env python3
"""Full enumeration + download of TCEQ CFR Title V documents (record series 1051).

Why this exists: CFR search pagination is capped at 10,000 rows per query, so
`download_tx_tceq_titlev_permits.py --scan-all` can only see the newest 10K of
~159K documents. CFR's xPrimaryID criterion is an UNANCHORED SUBSTRING match,
so querying every 2-digit string '00'..'99' covers every numeric permit ID
(each contains at least one digram); buckets that near the cap are subdivided
by a third digit. Results are deduped on dDocName.

Stages (both resumable):
    python scripts/tx_cfr_sweep.py enumerate   # -> cfr_sweep_docs.jsonl + tx_cfr_full_index.csv
    python scripts/tx_cfr_sweep.py download    # title-filter + latest-per-permit, stream PDFs

Output layout (shared with the main downloader):
    <RAW_DATA_DIR>/tx_tceq_titlev_permits/
        cfr_sweep_docs.jsonl      one record per (bucket) with its rows
        tx_cfr_full_index.csv     deduped full document index
        pdfs/                     downloaded PDFs
        tx_cfr_download_log.csv   per-document download status
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from loguru import logger

from download_tx_tceq_titlev_permits import (
    ALLOWED_DOC_TITLES,
    DEFAULT_OUTPUT_DIR,
    build_session,
    cfr_doc_filename,
    cfr_download,
    cfr_extract_rows,
    cfr_get_access,
    cfr_search,
    cfr_total_rows,
    latest_per_permit_title,
)

SWEEP_JSONL = DEFAULT_OUTPUT_DIR / "cfr_sweep_docs.jsonl"
FULL_INDEX = DEFAULT_OUTPUT_DIR / "tx_cfr_full_index.csv"
DL_LOG = DEFAULT_OUTPUT_DIR / "tx_cfr_download_log.csv"
CAP = 10_000          # CFR pagination ceiling
SUBDIVIDE_AT = 9_500  # subdivide a bucket before it can hit the ceiling
PAGE = 200


def paginate(session, access_id, client_ip, needle, timeout, delay):
    """Yield all rows for one substring bucket (must be < CAP total)."""
    start = 1
    while True:
        payload = cfr_search(session, access_id, client_ip,
                             primary_id=needle, result_count=PAGE,
                             start_row=start, timeout=timeout)
        rows = cfr_extract_rows(payload)
        if not rows:
            return
        yield from rows
        start += len(rows)
        total = cfr_total_rows(payload)
        if start > min(total, CAP):
            return
        if delay:
            time.sleep(delay)


def enumerate_cmd(args):
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    done_buckets = set()
    if SWEEP_JSONL.exists():
        for line in SWEEP_JSONL.read_text().splitlines():
            try:
                done_buckets.add(json.loads(line)["bucket"])
            except (json.JSONDecodeError, KeyError):
                continue

    session = build_session(args.retries)
    access_id, client_ip = cfr_get_access(session, args.timeout)
    queue = [f"{i:02d}" for i in range(100)]
    n_done = 0
    with SWEEP_JSONL.open("a") as out:
        while queue:
            bucket = queue.pop(0)
            if bucket in done_buckets:
                continue
            probe = cfr_search(session, access_id, client_ip,
                               primary_id=bucket, result_count=1,
                               timeout=args.timeout)
            total = cfr_total_rows(probe)
            if total >= SUBDIVIDE_AT:
                children = [bucket + d for d in "0123456789"]
                logger.warning(f"bucket {bucket}: {total} rows >= {SUBDIVIDE_AT}, "
                               f"subdividing -> {children}")
                queue.extend(children)
                out.write(json.dumps({"bucket": bucket, "subdivided": True,
                                      "total": total}) + "\n")
                out.flush()
                continue
            rows = list(paginate(session, access_id, client_ip, bucket,
                                 args.timeout, args.delay))
            out.write(json.dumps({"bucket": bucket, "total": total,
                                  "rows": rows}) + "\n")
            out.flush()
            n_done += 1
            logger.info(f"bucket {bucket}: {len(rows)} rows ({n_done} buckets this run)")
            if args.delay:
                time.sleep(args.delay)

    # consolidate -> deduped full index
    seen = {}
    for line in SWEEP_JSONL.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for r in rec.get("rows") or []:
            key = r.get("dDocName") or r.get("doc_name")
            if key and key not in seen:
                seen[key] = r
    docs = list(seen.values())
    if docs:
        cols = sorted({k for d in docs for k in d})
        with FULL_INDEX.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(docs)
    logger.info(f"full index: {len(docs)} distinct documents -> {FULL_INDEX}")


def download_cmd(args):
    docs = list(csv.DictReader(FULL_INDEX.open()))
    logger.info(f"full index: {len(docs)} documents")
    keep_titles = set(args.titles)
    docs = [d for d in docs if (d.get("dDocTitle") or d.get("doc_title")) in keep_titles]
    logger.info(f"title filter {sorted(keep_titles)}: -> {len(docs)}")
    if args.latest_only:
        docs = latest_per_permit_title(docs)
        logger.info(f"latest-only dedup: -> {len(docs)}")
    if args.max:
        docs = docs[: args.max]

    already = set()
    if DL_LOG.exists():
        for row in csv.DictReader(DL_LOG.open()):
            if row.get("status") == "downloaded":
                already.add(row["doc_name"])

    pdf_dir = DEFAULT_OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    session = build_session(args.retries)
    new_log = not DL_LOG.exists()
    n_ok = n_err = n_skip = 0
    with DL_LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["doc_name", "permit_number", "doc_title",
                                          "filename", "status", "size_bytes", "error"])
        if new_log:
            w.writeheader()
        for i, doc in enumerate(docs, 1):
            doc_name = doc.get("dDocName") or doc.get("doc_name") or ""
            if not doc_name or doc_name in already:
                n_skip += 1
                continue
            permit = doc.get("xPrimaryID") or doc.get("permit_number") or ""
            fname = cfr_doc_filename(permit, doc)
            dest = pdf_dir / fname
            if dest.exists() and dest.stat().st_size > 0:
                status, size, err = "downloaded", dest.stat().st_size, ""
            else:
                try:
                    size = cfr_download(session, doc_name, dest, args.timeout)
                    status, err = "downloaded", ""
                except Exception as e:
                    status, size, err = "error", 0, f"{type(e).__name__}: {e}"
            w.writerow({"doc_name": doc_name, "permit_number": permit,
                        "doc_title": doc.get("dDocTitle") or doc.get("doc_title"),
                        "filename": fname, "status": status,
                        "size_bytes": size, "error": err})
            f.flush()
            if status == "downloaded":
                n_ok += 1
            else:
                n_err += 1
                logger.warning(f"[{i}/{len(docs)}] {doc_name}: {err}")
            if i % 100 == 0:
                logger.info(f"[{i}/{len(docs)}] ok={n_ok} err={n_err} skip={n_skip}")
            if args.delay:
                time.sleep(args.delay)
    logger.info(f"done: ok={n_ok} err={n_err} skipped(existing)={n_skip}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("enumerate", enumerate_cmd), ("download", download_cmd)]:
        p = sub.add_parser(name)
        p.add_argument("--delay", type=float, default=0.3)
        p.add_argument("--timeout", type=int, default=120)
        p.add_argument("--retries", type=int, default=5)
        if name == "download":
            p.add_argument("--titles", nargs="+", default=list(ALLOWED_DOC_TITLES))
            p.add_argument("--latest-only", action="store_true", default=True)
            p.add_argument("--no-latest-only", action="store_false", dest="latest_only")
            p.add_argument("--max", type=int, default=None)
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
