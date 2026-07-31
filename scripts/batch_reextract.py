"""Batch re-extraction with forced chunking, parallel workers, and resume.

Processes the recall-suspect + failed files list produced by
check_unit_recall_by_size.py. Each file goes through forced chunked extraction
(recursively splitting chunks that truncate). Results append to a JSONL as
they complete, so the run can be killed and resumed — files already present in
the JSONL are skipped.

Usage:
    PYTHONPATH=. LLM_MODEL=gemini-2.5-flash LLM_MAX_OUTPUT_TOKENS=65536 \
    LLM_TIMEOUT_SECONDS=480 LLM_HARD_TIMEOUT_SECONDS=540 \
        python scripts/batch_reextract.py data/processed/reextraction/batch_list.json \
            --workers 8

    # after completion (or anytime), build the rows CSV:
    python scripts/batch_reextract.py --finalize

Abort-safety: if 15 consecutive files fail (e.g. CBORG IP authorization
expired -> 403 on everything), the run stops so it can be resumed after re-auth.
"""

import argparse
import json
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

from permit_data_extraction.dataset import (
    GENERAL_TARGET_FIELDS,
    LLM_MODEL,
    TEXT_INPUT_DIR,
    UNIT_DETAIL_FIELDS,
    _merge_chunk_extractions,
    configure_llm,
    postprocess_extraction_row,
    read_text_from_file,
)
from scripts.reextract_files_chunked import CHUNK_CHARS, _extract_recursive

OUT_DIR = Path("data/processed/reextraction")
RESULTS_JSONL = OUT_DIR / "batch_results.jsonl"
FINAL_CSV = OUT_DIR / "batch_reextract_rows.csv"
MAX_CONSECUTIVE_FAILURES = 15

_write_lock = threading.Lock()
_consec_failures = 0
_stop = threading.Event()


def _load_success_records() -> dict:
    """Last successful extraction record per filename. Error records (e.g. budget
    429s, auth expiry) do NOT count as done, so they retry on resume. Later
    success records supersede earlier ones (second-pass reruns)."""
    done = {}
    if RESULTS_JSONL.exists():
        for line in RESULTS_JSONL.read_text().splitlines():
            try:
                rec = json.loads(line)
                if "extraction" in rec:
                    done[rec["filename"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def _append_result(record: dict):
    with _write_lock:
        with open(RESULTS_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def _process_one(client, fn: str, idx: int, total: int, prior: dict = None):
    global _consec_failures
    if _stop.is_set():
        return
    path = TEXT_INPUT_DIR / f"{fn}.txt"
    started = datetime.now().isoformat(timespec="seconds")
    try:
        text = read_text_from_file(path)
        if not text:
            _append_result({"filename": fn, "error": "empty text", "started": started})
            return
        chunk_results, n_failed = _extract_recursive(client, text, fn, LLM_MODEL, CHUNK_CHARS)
        if prior:  # second pass: keep everything the first pass found
            chunk_results = [prior] + chunk_results
        if not chunk_results:
            with _write_lock:
                _consec_failures += 1
                if _consec_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"!! {MAX_CONSECUTIVE_FAILURES} consecutive failures — stopping (resume after fixing auth/API)")
                    _stop.set()
            _append_result({"filename": fn, "error": "no chunk succeeded", "started": started})
            print(f"[{idx}/{total}] {fn}: FAILED (no chunk succeeded)")
            return
        merged = _merge_chunk_extractions(chunk_results)
        with _write_lock:
            _consec_failures = 0
        n_units = len(merged.get("Emission Units") or [])
        _append_result({
            "filename": fn,
            "started": started,
            "model": LLM_MODEL,
            "doc_chars": len(text),
            "pieces_ok": len(chunk_results),
            "pieces_failed": n_failed,
            "n_units": n_units,
            "extraction": merged,
        })
        print(f"[{idx}/{total}] {fn}: {n_units} units ({len(chunk_results)} pieces ok, {n_failed} failed)")
    except Exception as e:  # keep the batch alive on any single-file explosion
        _append_result({"filename": fn, "error": f"{type(e).__name__}: {e}", "started": started})
        print(f"[{idx}/{total}] {fn}: ERROR {e}")


def finalize():
    """Convert batch_results.jsonl into a per-unit rows CSV (pipeline columns).
    Last success record per filename wins (second-pass reruns supersede)."""
    rows = []
    success = _load_success_records()
    all_names = set()
    for line in RESULTS_JSONL.read_text().splitlines():
        try:
            all_names.add(json.loads(line)["filename"])
        except (json.JSONDecodeError, KeyError):
            continue
    n_err = len(all_names - set(success))
    n_files = len(success)
    for rec in success.values():
        extracted = rec["extraction"]
        general = {f: extracted.get(f) for f in GENERAL_TARGET_FIELDS}
        model_used = f"{rec.get('model', LLM_MODEL)} (forced-chunked)"
        units = extracted.get("Emission Units") or []
        for unit in units:
            if not isinstance(unit, dict):
                continue
            row = {"Filename": rec["filename"], "Status": "Success", "Model Used": model_used,
                   "Pieces Failed": rec.get("pieces_failed", 0)}
            row.update(general)
            for f in UNIT_DETAIL_FIELDS:
                row[f] = unit.get(f)
            rows.append(postprocess_extraction_row(row))
        if not units:
            row = {"Filename": rec["filename"], "Status": "Success (No Units Found)",
                   "Model Used": model_used, "Pieces Failed": rec.get("pieces_failed", 0)}
            row.update(general)
            rows.append(postprocess_extraction_row(row))
    pd.DataFrame(rows).to_csv(FINAL_CSV, index=False)
    print(f"Finalized {n_files} files ({n_err} errored) -> {len(rows)} rows -> {FINAL_CSV}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("list_file", nargs="?", help="JSON list of filename stems")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--finalize", action="store_true", help="only build the rows CSV from existing JSONL")
    ap.add_argument("--second-pass", action="store_true",
                    help="re-run completed files that still have failed pieces; "
                    "merges the new extraction with the prior one")
    ap.add_argument("--results-jsonl", type=Path, default=None,
                    help="use a separate results/checkpoint file (default: batch_results.jsonl); "
                    "the rows CSV is written next to it as <stem>_rows.csv")
    args = ap.parse_args()

    if args.results_jsonl:
        global RESULTS_JSONL, FINAL_CSV
        RESULTS_JSONL = args.results_jsonl
        FINAL_CSV = args.results_jsonl.with_name(args.results_jsonl.stem + "_rows.csv")

    if args.finalize:
        finalize()
        return

    done = _load_success_records()
    if args.second_pass:
        prior = {fn: rec for fn, rec in done.items() if rec.get("pieces_failed", 0) > 0}
        todo = sorted(prior)
        print(f"second pass: {len(todo)} files with failed pieces | model={LLM_MODEL} workers={args.workers}")
    else:
        prior = {}
        filenames = json.loads(Path(args.list_file).read_text())
        todo = [fn for fn in filenames if fn not in done]
        print(f"{len(filenames)} in list | {len(done)} already done | {len(todo)} to process | "
              f"model={LLM_MODEL} workers={args.workers}")
    if not todo:
        finalize()
        return

    client = configure_llm()
    if not client:
        raise SystemExit("LLM configuration failed")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, fn in enumerate(todo, 1):
            ex.submit(_process_one, client, fn, i, len(todo),
                      prior.get(fn, {}).get("extraction"))

    if _stop.is_set():
        print("Stopped early due to consecutive failures. Re-run the same command to resume.")
    else:
        finalize()


if __name__ == "__main__":
    main()
