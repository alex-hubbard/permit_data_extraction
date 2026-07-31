"""Recover text from PDFs that the original PyPDF2-based pipeline failed on.

The 826 files in batch_skipped_no_text.json failed the original PDF->text step
(quarantined to data/raw/processed/by_state/ERROR/). Spot-checks show poppler's
pdftotext extracts most of them cleanly. This script:

  1. runs pdftotext over each PDF (full document),
  2. writes data/interim/extracted_text/<fn>.txt using the pipeline's
     "--- Page Break ---" convention (so downstream code sees a normal file),
  3. classifies near-empty output as needs_ocr (image-only scans) and does NOT
     write a text file for those — they get a separate OCR pass.

Resumable: progress checkpoints to notext_recovery.jsonl; filenames with a
terminal record (ok / needs_ocr / error other than timeout) are skipped on
rerun. Timeouts retry with a longer budget.

Usage:
    python3 scripts/recover_no_text_pdfs.py [--workers 6] [--limit N]
"""
import argparse
import json
import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

SKIPPED_JSON = Path("data/processed/reextraction/batch_skipped_no_text.json")
ERROR_PDF_DIR = Path("data/raw/processed/by_state/ERROR")
TEXT_OUT_DIR = Path("data/interim/extracted_text")
MANIFEST = Path("data/processed/reextraction/notext_recovery.jsonl")
PDFTOTEXT_TIMEOUT = 600  # seconds; some PDFs are 200MB

_write_lock = threading.Lock()


def _append(record):
    with _write_lock:
        with open(MANIFEST, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def _load_done():
    """Filenames with a terminal record. Timeout errors retry on rerun."""
    done = set()
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") in ("ok", "needs_ocr") or (
                rec.get("status") == "error" and rec.get("error") != "timeout"
            ):
                done.add(rec["filename"])
    return done


def _process_one(fn, idx, total):
    pdf = ERROR_PDF_DIR / (fn + ".pdf")
    out_txt = TEXT_OUT_DIR / (fn + ".txt")
    started = datetime.now().isoformat(timespec="seconds")
    if out_txt.exists():
        _append({"filename": fn, "status": "ok", "method": "preexisting",
                 "chars": out_txt.stat().st_size, "started": started})
        print(f"[{idx}/{total}] {fn}: already has text, skipping")
        return
    if not pdf.exists():
        _append({"filename": fn, "status": "error", "error": "pdf not found",
                 "started": started})
        print(f"[{idx}/{total}] {fn}: PDF NOT FOUND")
        return
    size = pdf.stat().st_size
    if size == 0:
        _append({"filename": fn, "status": "error", "error": "zero-byte pdf",
                 "started": started})
        print(f"[{idx}/{total}] {fn}: ZERO-BYTE PDF")
        return
    try:
        r = subprocess.run(
            ["pdftotext", "-q", str(pdf), "-"],
            capture_output=True, timeout=PDFTOTEXT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _append({"filename": fn, "status": "error", "error": "timeout",
                 "pdf_bytes": size, "started": started})
        print(f"[{idx}/{total}] {fn}: TIMEOUT ({size/1e6:.0f}MB)")
        return
    if r.returncode != 0:
        _append({"filename": fn, "status": "error",
                 "error": f"pdftotext rc={r.returncode}: {r.stderr.decode()[:200]}",
                 "pdf_bytes": size, "started": started})
        print(f"[{idx}/{total}] {fn}: pdftotext FAILED rc={r.returncode}")
        return
    raw = r.stdout.decode("utf-8", "ignore")
    pages = raw.count("\f") or 1
    text = raw.replace("\f", "\n\n--- Page Break ---\n\n")
    stripped = len(raw.replace("\f", "").strip())
    # Same spirit as the original pipeline's min-chars-per-page OCR trigger.
    if stripped < max(300, 25 * pages):
        _append({"filename": fn, "status": "needs_ocr", "pdf_bytes": size,
                 "pages": pages, "chars": stripped, "started": started})
        print(f"[{idx}/{total}] {fn}: needs OCR ({stripped} chars / {pages} pages)")
        return
    tmp = out_txt.with_suffix(".txt.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.rename(out_txt)
    _append({"filename": fn, "status": "ok", "method": "pdftotext",
             "pdf_bytes": size, "pages": pages, "chars": len(text),
             "started": started})
    print(f"[{idx}/{total}] {fn}: OK ({len(text)} chars, {pages} pages)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    TEXT_OUT_DIR.mkdir(parents=True, exist_ok=True)
    skipped = json.loads(SKIPPED_JSON.read_text())
    done = _load_done()
    todo = [f for f in skipped if f not in done]
    if args.limit:
        todo = todo[: args.limit]
    total = len(todo)
    print(f"{len(skipped)} listed | {len(done)} already done | {total} to process")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, fn in enumerate(todo, 1):
            ex.submit(_process_one, fn, i, total)
    # summary
    counts = {}
    for line in MANIFEST.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        counts[rec.get("status")] = counts.get(rec.get("status"), 0) + 1
    print("manifest status counts (incl. superseded records):", counts)


if __name__ == "__main__":
    main()
