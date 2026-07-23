"""Local OCR for raw (image-only) LDEQ EDMS downloads.

Companion to download_la_edms_titlev_permits.py running WITHOUT --ocr:
scans pdfs_raw/ for PDFs with no terminal manifest record, runs ocrmypdf,
writes the searchable PDF to pdfs/ (same corpus as the server-OCR'd docs)
and sidecar text to data/interim/extracted_text/<stem>.txt with the
pipeline's "--- Page Break ---" convention.

LDEQ scans have many 180-degree-rotated pages; the default OSD confidence
threshold (14) leaves table-heavy rotated pages upside down, which OCRs to
gibberish — hence --rotate-pages with threshold 2 (validated 2026-07-23
against 3 server-OCR'd docs: page-aligned bag-of-words similarity ~0.93).

One pass over what's currently downloaded, then exit 0; run under a
Restart=always systemd unit so new downloads get picked up every rescan.

Usage:
    python3 scripts/ocr_la_raw_pdfs.py [--jobs 10] [--limit N]
"""
import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

DATASET_DIR = Path("data/raw/la_edms_titlev_permits")
RAW_PDF_DIR = DATASET_DIR / "pdfs_raw"
OCR_PDF_DIR = DATASET_DIR / "pdfs"
MANIFEST = DATASET_DIR / "la_ocr_manifest.jsonl"
TEXT_OUT_DIR = Path("data/interim/extracted_text")

os.environ.setdefault("TESSDATA_PREFIX", "/usr/share/tesseract-ocr/4.00/tessdata")


def _append(record):
    with open(MANIFEST, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _load_done():
    """Filenames with a terminal record. OCR timeouts retry on rerun."""
    done = set()
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok" or (
                rec.get("status") == "error" and rec.get("error") != "ocr timeout"
            ):
                done.add(rec["filename"])
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=10, help="ocrmypdf page parallelism")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    OCR_PDF_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_OUT_DIR.mkdir(parents=True, exist_ok=True)
    done = _load_done()
    todo = sorted(
        p for p in RAW_PDF_DIR.glob("*.pdf")
        if p.name not in done
        and not ((OCR_PDF_DIR / p.name).exists()
                 and (TEXT_OUT_DIR / (p.stem + ".txt")).exists())
    )
    if args.limit:
        todo = todo[: args.limit]
    # smallest first so progress is visible early
    todo.sort(key=lambda p: p.stat().st_size)
    total = len(todo)
    print(f"{total} raw PDFs to OCR, jobs={args.jobs}", flush=True)

    for i, pdf in enumerate(todo, 1):
        started = datetime.now().isoformat(timespec="seconds")
        mb = pdf.stat().st_size / 1e6
        timeout = int(1800 + mb * 120)
        out_pdf = OCR_PDF_DIR / pdf.name
        out_txt = TEXT_OUT_DIR / (pdf.stem + ".txt")
        with tempfile.TemporaryDirectory() as td:
            sidecar = Path(td) / "sidecar.txt"
            tmp_pdf = Path(td) / "out.pdf"
            try:
                r = subprocess.run(
                    ["ocrmypdf", "--sidecar", str(sidecar), "--output-type", "pdf",
                     "--optimize", "0", "--skip-text", "--rotate-pages",
                     "--rotate-pages-threshold", "2", "--jobs", str(args.jobs),
                     str(pdf), str(tmp_pdf)],
                    capture_output=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                _append({"filename": pdf.name, "status": "error",
                         "error": "ocr timeout", "mb": round(mb, 1),
                         "started": started})
                print(f"[{i}/{total}] {pdf.name}: OCR TIMEOUT ({mb:.0f} MB)", flush=True)
                continue
            if r.returncode != 0 or not sidecar.exists():
                _append({"filename": pdf.name, "status": "error",
                         "error": f"ocrmypdf rc={r.returncode}: {r.stderr.decode()[-300:]}",
                         "mb": round(mb, 1), "started": started})
                print(f"[{i}/{total}] {pdf.name}: ocrmypdf FAILED rc={r.returncode}", flush=True)
                continue
            text = sidecar.read_text(encoding="utf-8", errors="ignore").replace(
                "\f", "\n\n--- Page Break ---\n\n")
            stripped = len(text.replace("--- Page Break ---", "").strip())
            if stripped < 300:
                _append({"filename": pdf.name, "status": "error",
                         "error": f"ocr produced only {stripped} chars",
                         "mb": round(mb, 1), "started": started})
                print(f"[{i}/{total}] {pdf.name}: OCR text too thin ({stripped} chars)", flush=True)
                continue
            staged = out_pdf.with_suffix(".pdf.part")
            staged.write_bytes(tmp_pdf.read_bytes())
            staged.replace(out_pdf)
        tmp_txt = out_txt.with_suffix(".txt.tmp")
        tmp_txt.write_text(text, encoding="utf-8")
        tmp_txt.rename(out_txt)
        _append({"filename": pdf.name, "status": "ok", "chars": len(text),
                 "mb": round(mb, 1), "started": started})
        print(f"[{i}/{total}] {pdf.name}: OK ({len(text)} chars, {mb:.0f} MB)", flush=True)


if __name__ == "__main__":
    main()
