#!/usr/bin/env python3
"""OCR + LLM-extract generator units from the scraped data-center permit PDFs.

Covers the state scrapes under data/raw/ (tx_tceq_dc_nsr_permits,
ga_epd_dc_permits, or_deq_dc_permits, nv_ndep_dc_permits,
az_maricopa_dc_permits). Georgia permit/narrative pairs are concatenated
before extraction — the permit carries conditions, the narrative carries the
unit ratings (make/model/kW), and only together do they extract completely.

Uses the existing pipeline pieces: ocr.extract_text_from_single_pdf and
dataset.process_text_file (set LLM_MODEL, e.g. gemini-flash — the CBORG
gateway no longer serves the old gemini-2.0-* names).

Results append to a JSONL as they complete, so a killed run resumes (files
already in the JSONL are skipped). --finalize builds the combined CSV.

Usage:
    LLM_MODEL=gemini-flash python scripts/extract_dc_generators.py [--states tx,ga] [--workers 4]
    python scripts/extract_dc_generators.py --finalize
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.config import INTERIM_DATA_DIR, PROCESSED_DATA_DIR, RAW_DATA_DIR
from permit_data_extraction.ocr import extract_text_from_single_pdf

STATE_DIRS = {
    "tx": RAW_DATA_DIR / "tx_tceq_dc_nsr_permits" / "pdfs",
    "ga": RAW_DATA_DIR / "ga_epd_dc_permits" / "pdfs",
    "or": RAW_DATA_DIR / "or_deq_dc_permits" / "pdfs",
    "nv": RAW_DATA_DIR / "nv_ndep_dc_permits" / "pdfs",
    "az": RAW_DATA_DIR / "az_maricopa_dc_permits" / "pdfs",
}

TEXT_DIR = INTERIM_DATA_DIR / "dc_generator_text"
OUT_DIR = PROCESSED_DATA_DIR / "dc_generators"
RESULTS_JSONL = OUT_DIR / "extraction_results.jsonl"
FINAL_CSV = OUT_DIR / "dc_generator_units.csv"
MAX_CONSECUTIVE_FAILURES = 15

_write_lock = threading.Lock()
_fail_lock = threading.Lock()
_consec_failures = 0
_stop = threading.Event()


def collect_jobs(states: List[str]) -> List[Tuple[str, str, List[Path]]]:
    """Return (state, job_key, [pdf paths]) — GA permit+narrative pairs merge."""
    jobs = []
    for state in states:
        pdf_dir = STATE_DIRS[state]
        if not pdf_dir.exists():
            logger.warning(f"{state}: {pdf_dir} missing, skipping")
            continue
        pdfs = sorted(pdf_dir.glob("*.pdf"))
        if state == "ga":
            by_stem: Dict[str, List[Path]] = {}
            for p in pdfs:
                stem = p.stem.replace("_narrative", "").replace("_permit", "")
                by_stem.setdefault(stem, []).append(p)
            for stem, paths in by_stem.items():
                # permit first, then narrative
                paths.sort(key=lambda p: 0 if "_permit" in p.stem else 1)
                jobs.append((state, f"ga_{stem}", paths))
        else:
            jobs.extend((state, f"{state}_{p.stem}", [p]) for p in pdfs)
    return jobs


def get_text(job_key: str, paths: List[Path]) -> str:
    txt_path = TEXT_DIR / f"{job_key}.txt"
    if txt_path.exists():
        return txt_path.read_text(errors="ignore")
    parts = []
    for p in paths:
        t = extract_text_from_single_pdf(p)
        if isinstance(t, tuple):
            t = t[0]
        parts.append(t or "")
    text = "\n\n=== ATTACHED DOCUMENT ===\n\n".join(parts)
    txt_path.write_text(text)
    return text


def _append(record: dict) -> None:
    with _write_lock:
        with open(RESULTS_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def process_job(client, state: str, job_key: str, paths: List[Path], idx: int, total: int) -> None:
    global _consec_failures
    if _stop.is_set():
        return
    from permit_data_extraction.dataset import process_text_file

    try:
        text = get_text(job_key, paths)
        if len(text.strip()) < 200:
            _append({"job": job_key, "state": state, "error": "empty/short text"})
            return
        rows = process_text_file(idx, total, TEXT_DIR / f"{job_key}.txt", client)
        # "Success (No Units Found)" is a valid outcome, not a failure.
        ok = any(str(r.get("Status", "")).startswith("Success") for r in rows)
        _append({
            "job": job_key, "state": state,
            "files": [str(p) for p in paths],
            "rows": rows, "ok": ok,
        })
        with _fail_lock:
            _consec_failures = 0 if ok else _consec_failures + 1
            if _consec_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error("Too many consecutive failures; stopping (resume later)")
                _stop.set()
    except Exception as e:  # noqa: BLE001
        _append({"job": job_key, "state": state, "error": str(e)})
        with _fail_lock:
            _consec_failures += 1
            if _consec_failures >= MAX_CONSECUTIVE_FAILURES:
                _stop.set()


def finalize() -> None:
    rows = []
    for line in RESULTS_JSONL.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for r in rec.get("rows", []):
            if r.get("Status") != "Success":
                continue
            r["source_state"] = rec["state"]
            r["source_job"] = rec["job"]
            rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(FINAL_CSV, index=False)
    logger.success(f"{len(df)} unit rows from {df['source_job'].nunique()} documents -> {FINAL_CSV}")
    if "Unit Quantity" in df:
        qty = pd.to_numeric(df["Unit Quantity"], errors="coerce").fillna(1)
        logger.info(f"Total generator units (sum of quantities): {qty.sum():,.0f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--states", default="ga,or,nv,tx,az")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="Cap job count (pilot runs)")
    ap.add_argument("--finalize", action="store_true")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    if args.finalize:
        finalize()
        return 0

    from permit_data_extraction.dataset import configure_llm

    done = set()
    if RESULTS_JSONL.exists():
        for line in RESULTS_JSONL.read_text().splitlines():
            try:
                done.add(json.loads(line)["job"])
            except (json.JSONDecodeError, KeyError):
                continue

    jobs = collect_jobs([s.strip() for s in args.states.split(",") if s.strip()])
    jobs = [j for j in jobs if j[1] not in done]
    if args.limit:
        jobs = jobs[: args.limit]
    logger.info(f"{len(jobs)} jobs to run ({len(done)} already done)")

    client = configure_llm()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [
            ex.submit(process_job, client, state, key, paths, i, len(jobs))
            for i, (state, key, paths) in enumerate(jobs, 1)
        ]
        for _ in as_completed(futs):
            pass

    logger.success("Run complete (or stopped); use --finalize to build the CSV")
    return 0


if __name__ == "__main__":
    sys.exit(main())
