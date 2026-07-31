"""Draw the GCP bakeoff sample: gemma-completed docs to re-extract with
candidate paid models and score via the content-agreement judge.

Samples from freemodel_results.jsonl successes with pieces_failed == 0 (clean
gemma baseline for apples-to-apples judging), stratified across doc-size
quintiles, text file required. Writes a lane-style CSV (filename column) that
build_vertex_batch_inputs.py consumes directly, with gemma's unit count and
doc size carried along for the scoring stage.

Usage: python scripts/build_bakeoff_sample.py [n] [seed]
Output: data/processed/gcp_batch/bakeoff_sample.csv
"""
import csv
import json
import random
import sys
from pathlib import Path

RESULTS = Path("data/processed/reextraction/freemodel_results.jsonl")
TEXT_DIR = Path("data/interim/extracted_text")
OUT = Path("data/processed/gcp_batch/bakeoff_sample.csv")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    pool = {}
    for line in RESULTS.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("n_units") is None or r.get("pieces_failed", 0) != 0:
            continue
        if not (TEXT_DIR / (r["filename"] + ".txt")).exists():
            continue
        pool[r["filename"]] = r  # last success per filename wins
    docs = sorted(pool.values(), key=lambda r: r.get("doc_chars", 0))
    if len(docs) < n:
        raise SystemExit(f"only {len(docs)} eligible docs (< {n})")
    per_bin, rem = divmod(n, 5)
    rng = random.Random(seed)
    picked = []
    bin_size = len(docs) / 5
    for b in range(5):
        seg = docs[int(b * bin_size):int((b + 1) * bin_size)]
        k = per_bin + (1 if b < rem else 0)
        picked.extend(rng.sample(seg, k))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "doc_chars", "gemma_n_units",
                                          "gemma_pieces_ok"])
        w.writeheader()
        for r in sorted(picked, key=lambda r: r.get("doc_chars", 0)):
            w.writerow({"filename": r["filename"], "doc_chars": r.get("doc_chars"),
                        "gemma_n_units": r.get("n_units"),
                        "gemma_pieces_ok": r.get("pieces_ok")})
    sizes = [r.get("doc_chars", 0) for r in picked]
    print(f"{OUT}: {len(picked)} docs, doc_chars {min(sizes):,}-{max(sizes):,} "
          f"(median {sorted(sizes)[len(sizes)//2]:,}), seed={seed}")


if __name__ == "__main__":
    main()
