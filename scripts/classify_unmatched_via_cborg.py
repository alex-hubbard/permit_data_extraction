"""Classify Industry Description strings that the curated regex rules in
``industry_description_classifier`` could not assign to a NAICS code.

For each unique unmatched description in the current workbook, this script
asks a small LLM (via the LBL CBORG OpenAI-compatible proxy) to return a
6-digit NAICS code. Results are written incrementally to
``permit_data_extraction/data/description_naics_cache.json`` so the
classifier picks them up on the next run and reruns are resumable.

Run from the repo root:

    python scripts/classify_unmatched_via_cborg.py
    python scripts/classify_unmatched_via_cborg.py --batch-size 25 --model lbl/llama
    python scripts/classify_unmatched_via_cborg.py --limit 200      # smoke test
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import openai
import pandas as pd
from dotenv import dotenv_values
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from permit_data_extraction.config import PROCESSED_DATA_DIR  # noqa: E402
from permit_data_extraction.industry_description_classifier import (  # noqa: E402
    classify_industry_to_naics,
)

CACHE_PATH = PROJECT_ROOT / "permit_data_extraction" / "data" / "description_naics_cache.json"
DEFAULT_MODEL = "gemini-2.0-flash-lite"
SYSTEM_PROMPT = (
    "You classify free-text industrial-facility descriptions to 6-digit NAICS "
    "(2022) codes. Reply with strict JSON only, no prose. The JSON is an object "
    "mapping each input id to a 6-digit NAICS code string. If a description is "
    'unclassifiable or ambiguous, use the value "UNK". Manufacturing codes start '
    'with 31, 32, or 33. Data centers are 518210. Water supply is 221310. '
    "Wastewater is 221320. Pipelines are 486xxx. Mining is 21xxxx. Power "
    "generation is 221112."
)


def load_cache() -> dict[str, str]:
    if CACHE_PATH.exists():
        with CACHE_PATH.open() as f:
            return json.load(f)
    return {}


def save_cache(cache: dict[str, str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
    tmp.replace(CACHE_PATH)


def collect_unmatched_descriptions(workbook: Path, cache: dict[str, str]) -> list[str]:
    df = pd.read_excel(workbook, sheet_name=None, engine="openpyxl")
    descs: set[str] = set()
    for tab_name, tab_df in df.items():
        col = "Industry Description"
        if col not in tab_df.columns:
            continue
        s = tab_df[col].dropna().astype(str).str.strip()
        for d in s:
            if d and classify_industry_to_naics(d) is None and d.lower() not in cache:
                descs.add(d)
    return sorted(descs)


def build_user_prompt(batch: list[str]) -> str:
    lines = [
        "Classify each description below to a 6-digit NAICS code or UNK.",
        'Reply with JSON like: {"1": "324110", "2": "486210", "3": "UNK"}',
        "",
    ]
    for i, d in enumerate(batch, 1):
        lines.append(f"{i}. {d}")
    return "\n".join(lines)


def call_cborg(client: openai.OpenAI, model: str, batch: list[str]) -> dict[int, str]:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(batch)},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content or "{}"
    parsed = json.loads(text)
    return {int(k): str(v).strip().upper() for k, v in parsed.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path,
                        default=PROCESSED_DATA_DIR / "permit_data_extracted.xlsx")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=None,
                        help="If set, only process this many unique descriptions (for smoke testing)")
    args = parser.parse_args()

    api_key = dotenv_values().get("CBORG_API_KEY") or os.getenv("CBORG_API_KEY")
    if not api_key:
        print("CBORG_API_KEY not set in environment or .env", file=sys.stderr)
        return 1

    client = openai.OpenAI(api_key=api_key, base_url="https://api.cborg.lbl.gov")

    cache = load_cache()
    print(f"Cache currently holds {len(cache):,} entries.")

    unmatched = collect_unmatched_descriptions(args.workbook, cache)
    if args.limit:
        unmatched = unmatched[: args.limit]
    print(f"Unique unmatched descriptions to classify: {len(unmatched):,}")
    if not unmatched:
        return 0

    batches = [unmatched[i:i + args.batch_size] for i in range(0, len(unmatched), args.batch_size)]
    classified = 0
    failed_batches = 0
    pbar = tqdm(total=len(unmatched), desc="cborg classify")
    for batch in batches:
        try:
            result = call_cborg(client, args.model, batch)
        except Exception as exc:  # noqa: BLE001
            failed_batches += 1
            pbar.write(f"  batch failed ({len(batch)} items): {exc}")
            pbar.update(len(batch))
            time.sleep(2)
            continue
        for idx, desc in enumerate(batch, 1):
            code = result.get(idx, "UNK")
            cache[desc.lower()] = None if code == "UNK" else code
            classified += 1
        pbar.update(len(batch))
        save_cache(cache)
    pbar.close()

    print(f"Done. Classified {classified:,} descriptions, {failed_batches:,} batches failed.")
    print(f"Cache now holds {len(cache):,} entries at {CACHE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
