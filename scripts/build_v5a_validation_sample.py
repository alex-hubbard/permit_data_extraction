"""Draw a validation sample representative of the RELEASED dataset.

Earlier validation panels sampled a corpus in which `lbl/llama` produced most
records; after the 2026-07/08 re-extraction campaigns the release is 70%
gemini-2.5-flash, so those panels no longer describe what ships. This samples
from the union itself, stratified by

  (a) the model that produced each permit's released rows, allocated
      proportionally to that model's share of released PERMITS, and
  (b) document-length tercile within each model stratum,

so the panel mirrors the release. It also emits, for each sampled permit, the
released extraction reconstructed from the union rows, so the panel compares
against what users actually get rather than a fresh re-run.

Outputs (data/processed/validation/):
    v5a_sample.json          filename list, for batch_reextract.py
    v5a_sample_manifest.csv  per-permit model, doc chars, stratum
    v5a_released.jsonl       released extraction per permit, results-JSONL shape

Usage: python scripts/build_v5a_validation_sample.py [n] [seed]
"""
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
csv.field_size_limit(sys.maxsize)

UNION = Path("data/processed/permit_data_union_v5a.csv")
TEXT_DIR = Path("data/interim/extracted_text")
OUT_DIR = Path("data/processed/validation")
GENERAL = ["Facility Name", "Owner/Operator Name", "Facility Address", "Facility City",
           "Facility State Abbreviation", "Facility Zip Code", "Facility County",
           "NAICS Code", "SIC Code", "Operating Hours", "Industry Description",
           "Permit Number", "Permit Type", "Issuance Date", "Expiration Date",
           "Regulatory Authority",
           "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)"]
UNIT = ["Unit ID", "Unit Description", "Unit Quantity", "Unit Make", "Unit Model",
        "Year of Manufacture", "Unit Type", "Pollutants", "Emission Limits",
        "Opacity Limit", "Throughput/Production Limit", "Control Device(s)",
        "Capacity Value", "Capacity Unit", "Fuel Type", "Rated Efficiency",
        "Annual Run Hours", "Generation Capacity", "Applicable NESHAP/NSPS Subpart"]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20260802
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    model_of, general_of, units_of = {}, {}, defaultdict(list)
    with UNION.open(encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            if r["Status"] != "Success":
                continue
            fn = r["Filename"]
            if fn not in model_of:
                model_of[fn] = (r.get("Model Used") or "unknown").split(" (")[0] or "unknown"
                general_of[fn] = {k: (r.get(k) or None) for k in GENERAL}
            units_of[fn].append({k: (r.get(k) or None) for k in UNIT})

    have_text = {fn: (TEXT_DIR / (fn + ".txt")).stat().st_size
                 for fn in model_of if (TEXT_DIR / (fn + ".txt")).exists()}
    print(f"{len(model_of):,} successful permits; {len(have_text):,} with source text")

    by_model = defaultdict(list)
    for fn, size in have_text.items():
        by_model[model_of[fn]].append((size, fn))
    total = sum(len(v) for v in by_model.values())

    rng = random.Random(seed)
    picked = []
    for model, items in sorted(by_model.items(), key=lambda kv: -len(kv[1])):
        quota = round(n * len(items) / total)
        if quota == 0:
            continue
        items.sort()
        per, rem = divmod(quota, 3)
        third = len(items) / 3
        for b in range(3):
            seg = items[int(b * third):int((b + 1) * third)]
            k = min(per + (1 if b < rem else 0), len(seg))
            for size, fn in rng.sample(seg, k):
                picked.append({"filename": fn, "model": model, "doc_chars": size,
                               "stratum": f"{model}|tercile{b+1}"})
    print(f"sampled {len(picked)} permits across {len(by_model)} model strata")

    (OUT_DIR / "v5a_sample.json").write_text(
        json.dumps(sorted(p["filename"] for p in picked), indent=1))
    with (OUT_DIR / "v5a_sample_manifest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "model", "doc_chars", "stratum"])
        w.writeheader()
        w.writerows(sorted(picked, key=lambda p: p["stratum"]))
    with (OUT_DIR / "v5a_released.jsonl").open("w", encoding="utf-8") as f:
        for p in picked:
            fn = p["filename"]
            units = [u for u in units_of[fn] if any(v for v in u.values())]
            f.write(json.dumps({
                "filename": fn, "model": p["model"], "doc_chars": p["doc_chars"],
                "n_units": len(units),
                "extraction": {**general_of[fn], "Emission Units": units},
            }) + "\n")

    counts = defaultdict(int)
    for p in picked:
        counts[p["model"]] += 1
    print("panel composition (released-model share of sample):")
    for m, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {m:<28} {c:>4} ({c/len(picked):.0%}; release {len(by_model[m])/total:.0%})")


if __name__ == "__main__":
    main()
