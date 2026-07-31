"""Draw a reproducible random validation sample from the release dataset.

Samples permit filenames from the source dataset that have an exact-match
extracted-text file available (required to re-run multi-model validation), and
writes a JSON list consumable by `scripts/rerun_validation_for_files.py`.

Source defaults to the union dataset (`permit_data_union.csv`, the most complete
+ recent snapshot); override with PERMIT_VALIDATION_SOURCE=<path>.

Usage: python scripts/build_validation_sample.py [n] [seed] [out.json]
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RELEASE = Path(os.getenv("PERMIT_VALIDATION_SOURCE", ROOT / "data" / "processed" / "permit_data_union.csv"))
TEXT_DIR = ROOT / "data" / "interim" / "extracted_text"


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "data" / "processed" / "validation" / "release_validation_sample.json"

    df = pd.read_csv(RELEASE, low_memory=False, usecols=["Filename"])
    permits = pd.Series(df["Filename"].dropna().unique())
    have_text = permits[permits.map(lambda f: (TEXT_DIR / f"{f}.txt").exists())]
    print(f"release permits: {len(permits)} | with exact text file: {len(have_text)}")

    sample = have_text.sample(n=min(n, len(have_text)), random_state=seed).tolist()
    out.write_text(json.dumps(sample, indent=1))
    print(f"  [write] {out}  ({len(sample)} permits, seed={seed})")


if __name__ == "__main__":
    main()
