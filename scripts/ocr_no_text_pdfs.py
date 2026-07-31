"""Phase 2 of no-text recovery: OCR the image-only PDFs.

Reads notext_recovery.jsonl for files whose latest record is needs_ocr, runs
ocrmypdf (sidecar text output, internal page parallelism), converts form feeds
to the pipeline's "--- Page Break ---" convention, and writes
data/interim/extracted_text/<fn>.txt. Appends ok/error records to the same
manifest, so recover_no_text_pdfs.py-style resume semantics apply and
queue_recovered_into_freemodel.py picks the results up unchanged.

Duplicate-source filenames (two list entries symlinked to one PDF) OCR once
and share the text.

Usage:
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/4.00/tessdata \
        python3 scripts/ocr_no_text_pdfs.py [--jobs 10] [--limit N] \
        [--manifest path/to/other_manifest.jsonl]

With --manifest, needs_ocr records are read from (and results appended to)
that file instead; each record's pdf_path locates the source PDF, falling
back to ERROR_PDF_DIR/<filename>.pdf.
"""
import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

MANIFEST = Path("data/processed/reextraction/notext_recovery.jsonl")
ERROR_PDF_DIR = Path("data/raw/processed/by_state/ERROR")
TEXT_OUT_DIR = Path("data/interim/extracted_text")

os.environ.setdefault("TESSDATA_PREFIX", "/usr/share/tesseract-ocr/4.00/tessdata")


def _append(record):
    with open(MANIFEST, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def main():
    global MANIFEST
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=10, help="ocrmypdf page parallelism")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    args = ap.parse_args()
    MANIFEST = args.manifest

    last = {}
    for line in MANIFEST.read_text().splitlines():
        try:
            rec = json.loads(line)
            last[rec["filename"]] = rec
        except json.JSONDecodeError:
            continue
    todo = sorted(fn for fn, rec in last.items() if rec["status"] == "needs_ocr")
    if args.limit:
        todo = todo[: args.limit]
    # smallest page counts first so progress is visible early
    todo.sort(key=lambda fn: last[fn].get("pages") or 0)
    total = len(todo)
    print(f"{total} files to OCR ({sum(last[fn].get('pages') or 0 for fn in todo)} pages), jobs={args.jobs}", flush=True)

    text_by_realpath = {}
    for i, fn in enumerate(todo, 1):
        rec_path = last[fn].get("pdf_path")
        pdf = Path(rec_path) if rec_path else ERROR_PDF_DIR / (fn + ".pdf")
        out_txt = TEXT_OUT_DIR / (fn + ".txt")
        started = datetime.now().isoformat(timespec="seconds")
        pages = last[fn].get("pages") or 0
        real = str(pdf.resolve())
        if real in text_by_realpath:
            text = text_by_realpath[real]
            method = "ocrmypdf (shared source)"
        else:
            timeout = 1800 + pages * 20
            with tempfile.TemporaryDirectory() as td:
                sidecar = Path(td) / "sidecar.txt"
                out_pdf = Path(td) / "out.pdf"
                try:
                    r = subprocess.run(
                        ["ocrmypdf", "--sidecar", str(sidecar), "--output-type", "pdf",
                         "--optimize", "0", "--skip-text", "--jobs", str(args.jobs),
                         str(pdf), str(out_pdf)],
                        capture_output=True, timeout=timeout,
                    )
                except subprocess.TimeoutExpired:
                    _append({"filename": fn, "status": "error", "error": "ocr timeout",
                             "pages": pages, "started": started})
                    print(f"[{i}/{total}] {fn}: OCR TIMEOUT ({pages} pages)", flush=True)
                    continue
                if r.returncode != 0 or not sidecar.exists():
                    _append({"filename": fn, "status": "error",
                             "error": f"ocrmypdf rc={r.returncode}: {r.stderr.decode()[-300:]}",
                             "pages": pages, "started": started})
                    print(f"[{i}/{total}] {fn}: ocrmypdf FAILED rc={r.returncode}", flush=True)
                    continue
                raw = sidecar.read_text(encoding="utf-8", errors="ignore")
            text = raw.replace("\f", "\n\n--- Page Break ---\n\n")
            text_by_realpath[real] = text
            method = "ocrmypdf"
        stripped = len(text.replace("--- Page Break ---", "").strip())
        if stripped < max(300, 10 * pages):
            _append({"filename": fn, "status": "error", "error": f"ocr produced only {stripped} chars",
                     "pages": pages, "started": started})
            print(f"[{i}/{total}] {fn}: OCR text too thin ({stripped} chars)", flush=True)
            continue
        tmp = out_txt.with_suffix(".txt.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.rename(out_txt)
        _append({"filename": fn, "status": "ok", "method": method, "pages": pages,
                 "chars": len(text), "started": started})
        print(f"[{i}/{total}] {fn}: OK ({len(text)} chars, {pages} pages)", flush=True)


if __name__ == "__main__":
    main()
