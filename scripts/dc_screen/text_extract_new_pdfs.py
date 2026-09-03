#!/usr/bin/env python3
"""Text-extract PDFs that have no text file yet (pdftotext, OCR fallback).

Usage: python scripts/dc_screen/text_extract_new_pdfs.py PDF_DIR TEXT_DIR --prefix tx_ [--ocr-pages 60] [--workers 4]
Writes TEXT_DIR/<prefix><pdf stem>.txt; PDFs whose pdftotext output is under
500 characters are OCR'd with ocrmypdf (--skip-text, sidecar), capped at
--ocr-pages pages. A manifest CSV is appended next to TEXT_DIR.
"""
import argparse, csv, os, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

def extract(pdf: Path, out: Path, ocr_pages: int):
    try:
        r = subprocess.run(['pdftotext', '-layout', str(pdf), '-'], capture_output=True, text=True, timeout=300)
        text = r.stdout if r.returncode == 0 else ''
    except Exception as e:  # noqa: BLE001
        text = ''
    method = 'pdftotext'
    if len(text.strip()) < 500:
        method = 'ocr'
        with tempfile.TemporaryDirectory() as td:
            side = Path(td) / 'side.txt'
            env = dict(os.environ, TESSDATA_PREFIX=os.environ.get('TESSDATA_PREFIX', '/usr/share/tesseract-ocr/4.00/tessdata'))
            try:
                subprocess.run(['ocrmypdf', '--skip-text', '--pages', f'1-{ocr_pages}', '--sidecar', str(side), '--output-type', 'none', str(pdf), '-'],
                               capture_output=True, timeout=1800, env=env)
                if side.exists():
                    t2 = side.read_text(errors='ignore')
                    if len(t2.strip()) > len(text.strip()):
                        text = t2
            except Exception:  # noqa: BLE001
                pass
    out.write_text(text, encoding='utf-8')
    return method, len(text)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf_dir'); ap.add_argument('text_dir'); ap.add_argument('--prefix', default='')
    ap.add_argument('--ocr-pages', type=int, default=60); ap.add_argument('--workers', type=int, default=4)
    a = ap.parse_args()
    pdf_dir, text_dir = Path(a.pdf_dir), Path(a.text_dir); text_dir.mkdir(parents=True, exist_ok=True)
    todo = [p for p in sorted(pdf_dir.glob('*.pdf')) if not (text_dir / f'{a.prefix}{p.stem}.txt').exists()]
    print(f'{len(todo)} PDFs to extract', flush=True)
    man = text_dir.parent / f'{text_dir.name}_extraction_manifest.csv'
    new = not man.exists()
    with open(man, 'a', newline='') as f, ThreadPoolExecutor(a.workers) as ex:
        w = csv.writer(f)
        if new: w.writerow(['pdf', 'text', 'method', 'chars', 'ts'])
        futs = {ex.submit(extract, p, text_dir / f'{a.prefix}{p.stem}.txt', a.ocr_pages): p for p in todo}
        n = 0
        for fut in as_completed(futs):
            p = futs[fut]; n += 1
            try: method, chars = fut.result()
            except Exception as e: method, chars = f'error:{e}', 0
            w.writerow([p.name, f'{a.prefix}{p.stem}.txt', method, chars, time.strftime('%F %T')]); f.flush()
            if n % 50 == 0: print(n, flush=True)
    print('done', n, flush=True)

if __name__ == '__main__':
    main()
