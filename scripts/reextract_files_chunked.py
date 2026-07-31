"""Re-extract specific permit files with FORCED chunking.

The main pipeline sends the whole document in one call and only falls back to
chunking on an explicit token-limit/truncation error — so a long document can
return a "successful" but silently incomplete unit list (see the ORNL INPLT
cross-check: Frito-Lay 83799407/83817221 missed their main boilers). This
script always splits the document into chunks and merges the per-chunk results,
so every section of the permit is actually read.

Usage:
    PYTHONPATH=. LLM_MODEL=gemini-2.5-flash LLM_TIMEOUT_SECONDS=120 \
        python scripts/reextract_files_chunked.py 83799407 83817221

    # or a JSON list file:
    PYTHONPATH=. python scripts/reextract_files_chunked.py --list suspects.json

Writes data/processed/reextraction/reextract_<timestamp>.csv (one row per unit,
same columns as the pipeline) plus a .raw.json with the merged extraction dicts.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from permit_data_extraction.dataset import (
    GENERAL_TARGET_FIELDS,
    LLM_MODEL,
    TEXT_INPUT_DIR,
    UNIT_DETAIL_FIELDS,
    _merge_chunk_extractions,
    _split_text_into_chunks,
    configure_llm,
    extract_info_with_model,
    postprocess_extraction_row,
    read_text_from_file,
)

CHUNK_CHARS = 80_000  # ~20-25K tokens per chunk
MIN_CHUNK_CHARS = 10_000  # stop splitting below this; give up on the piece instead
OUT_DIR = Path("data/processed/reextraction")


def _extract_recursive(client, text: str, filename: str, model: str, chunk_chars: int) -> tuple[list[dict], int]:
    """Extract from text, splitting any chunk that truncates (finish_reason=length)
    or times out (dense-table regions drive runaway generation) into halves until
    it fits the output budget. Returns (chunk_results, n_failed_pieces).
    """
    results, failed = [], 0
    for i, chunk in enumerate(_split_text_into_chunks(text, chunk_chars), 1):
        label = f"{filename}_c{chunk_chars//1000}k_{i}"
        data, err = extract_info_with_model(client, chunk, label, model)
        err_s = str(err).lower()
        if data is not None:
            results.append(data)
        elif ("truncated" in err_s or "timed out" in err_s or "timeout" in err_s) and chunk_chars // 2 >= MIN_CHUNK_CHARS:
            print(f"    {label}: {'truncated' if 'truncated' in err_s else 'timed out'} -> splitting to {chunk_chars//2:,}-char pieces")
            sub_results, sub_failed = _extract_recursive(client, chunk, filename, model, chunk_chars // 2)
            results.extend(sub_results)
            failed += sub_failed
        else:
            print(f"    {label}: FAILED ({err})")
            failed += 1
    return results, failed


def rows_from_extraction(filename: str, extracted: dict, model_used: str) -> list[dict]:
    general = {f: extracted.get(f) for f in GENERAL_TARGET_FIELDS}
    units = extracted.get("Emission Units") or []
    rows = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        row = {"Filename": filename, "Status": "Success", "Model Used": model_used}
        row.update(general)
        for f in UNIT_DETAIL_FIELDS:
            row[f] = unit.get(f)
        rows.append(postprocess_extraction_row(row))
    if not rows:
        row = {"Filename": filename, "Status": "Success (No Units Found)", "Model Used": model_used}
        row.update(general)
        rows.append(postprocess_extraction_row(row))
    return rows


def main():
    args = sys.argv[1:]
    if args and args[0] == "--list":
        filenames = json.loads(Path(args[1]).read_text())
    else:
        filenames = args
    if not filenames:
        sys.exit("No filenames given. Usage: reextract_files_chunked.py <stem> [<stem> ...]")

    client = configure_llm()
    if not client:
        sys.exit("LLM configuration failed")

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = OUT_DIR / f"reextract_{stamp}.csv"
    out_raw = OUT_DIR / f"reextract_{stamp}.raw.json"

    all_rows, raw = [], {}
    for i, fn in enumerate(filenames, 1):
        path = TEXT_INPUT_DIR / f"{fn}.txt"
        if not path.exists():
            print(f"[{i}/{len(filenames)}] MISSING text file: {fn}")
            continue
        text = read_text_from_file(path)
        print(f"[{i}/{len(filenames)}] {fn}: {len(text):,} chars -> forced chunked extraction ({CHUNK_CHARS:,}/chunk, model {LLM_MODEL})")
        chunk_results, n_failed = _extract_recursive(client, text, fn, LLM_MODEL, CHUNK_CHARS)
        if not chunk_results:
            print("  FAILED: no chunk succeeded")
            raw[fn] = {"error": "no chunk succeeded"}
            continue
        extracted = _merge_chunk_extractions(chunk_results)
        n_units = len(extracted.get("Emission Units") or [])
        print(f"  -> {n_units} emission units from {len(chunk_results)} pieces ({n_failed} pieces failed)")
        extracted["_pieces_failed"] = n_failed
        raw[fn] = extracted
        all_rows.extend(rows_from_extraction(fn, extracted, f"{LLM_MODEL} (forced-chunked)"))

    out_raw.write_text(json.dumps(raw, indent=2))
    if all_rows:
        pd.DataFrame(all_rows).to_csv(out_csv, index=False)
        print(f"\nWrote {len(all_rows)} rows to {out_csv}")
    print(f"Raw extractions: {out_raw}")


if __name__ == "__main__":
    main()
