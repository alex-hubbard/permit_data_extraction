"""Bulk pdftotext extraction for newly scraped permit PDF directories.

Generalizes recover_no_text_pdfs.py: takes one or more PDF directories,
writes data/interim/extracted_text/<stem>.txt with the pipeline's
"--- Page Break ---" convention, and classifies near-empty output as
needs_ocr (image-only scans, no text file written — run
scripts/ocr_no_text_pdfs.py style pass on those separately).

Resumable: progress checkpoints to the --manifest jsonl; filenames with a
terminal record (ok / needs_ocr / error other than timeout) are skipped on
rerun. Timeouts retry with the same budget on rerun.

Usage:
    python3 scripts/extract_text_from_pdf_dirs.py \
        data/raw/tx_tceq_titlev_permits/pdfs data/raw/wi_dnr_titlev_permits/pdfs \
        --manifest data/processed/reextraction/txwi_text_extraction.jsonl \
        [--workers 6] [--limit N]
"""
import argparse
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

TEXT_OUT_DIR = Path("data/interim/extracted_text")
PDFTOTEXT_TIMEOUT = 600  # seconds; some permits are 200MB

_write_lock = threading.Lock()


def _append(manifest, record):
    with _write_lock:
        with open(manifest, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def _load_done(manifest):
    """Filenames with a terminal record. Timeout errors retry on rerun."""
    done = set()
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") in ("ok", "needs_ocr") or (
                rec.get("status") == "error" and rec.get("error") != "timeout"
            ):
                done.add(rec["filename"])
    return done


def _process_one(manifest, pdf, idx, total):
    fn = pdf.stem
    out_txt = TEXT_OUT_DIR / (fn + ".txt")
    started = datetime.now().isoformat(timespec="seconds")
    if out_txt.exists():
        _append(manifest, {"filename": fn, "status": "ok", "method": "preexisting",
                           "chars": out_txt.stat().st_size, "started": started})
        print(f"[{idx}/{total}] {fn}: already has text, skipping")
        return
    size = pdf.stat().st_size
    if size == 0:
        _append(manifest, {"filename": fn, "status": "error", "error": "zero-byte pdf",
                           "started": started})
        print(f"[{idx}/{total}] {fn}: ZERO-BYTE PDF")
        return
    try:
        r = subprocess.run(
            ["pdftotext", "-q", str(pdf), "-"],
            capture_output=True, timeout=PDFTOTEXT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _append(manifest, {"filename": fn, "status": "error", "error": "timeout",
                           "pdf_bytes": size, "started": started})
        print(f"[{idx}/{total}] {fn}: TIMEOUT ({size/1e6:.0f}MB)")
        return
    if r.returncode != 0:
        _append(manifest, {"filename": fn, "status": "error",
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
        _append(manifest, {"filename": fn, "status": "needs_ocr", "pdf_bytes": size,
                           "pdf_path": str(pdf), "pages": pages, "chars": stripped,
                           "started": started})
        print(f"[{idx}/{total}] {fn}: needs OCR ({stripped} chars / {pages} pages)")
        return
    tmp = out_txt.with_suffix(".txt.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.rename(out_txt)
    _append(manifest, {"filename": fn, "status": "ok", "method": "pdftotext",
                       "pdf_bytes": size, "pdf_path": str(pdf), "pages": pages,
                       "chars": len(text), "started": started})
    print(f"[{idx}/{total}] {fn}: OK ({len(text)} chars, {pages} pages)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_dirs", nargs="+", type=Path)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    TEXT_OUT_DIR.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    pdfs = []
    for d in args.pdf_dirs:
        pdfs.extend(sorted(d.glob("*.pdf")))
    stems = [p.stem for p in pdfs]
    dupes = {s for s in stems if stems.count(s) > 1}
    if dupes:
        raise SystemExit(f"stem collision across input dirs: {sorted(dupes)[:5]} ...")
    done = _load_done(args.manifest)
    todo = [p for p in pdfs if p.stem not in done]
    if args.limit:
        todo = todo[: args.limit]
    total = len(todo)
    print(f"{len(pdfs)} listed | {len(done)} already done | {total} to process")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, p in enumerate(todo, 1):
            ex.submit(_process_one, args.manifest, p, i, total)
    counts = {}
    for line in args.manifest.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        counts[rec.get("status")] = counts.get(rec.get("status"), 0) + 1
    print("manifest totals:", counts)


if __name__ == "__main__":
    main()
