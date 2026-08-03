"""Assemble the v5a validation panel into a judge-run JSONL.

Panel per permit:
  A = the RELEASED extraction, reconstructed from union rows (v5a_released.jsonl)
  B = independent re-extraction by anthropic/claude-sonnet
  C = independent re-extraction by google/glm-5

The adjudicator (run separately by judge_validation_agreement.py) performs no
extraction in this panel, so its rulings are independent of all three.

Only permits with at least two usable extractions are emitted, matching the
judge's own requirement.

Usage:
    PYTHONPATH=. python scripts/assemble_v5a_judge_run.py \
        [--out data/processed/validation/v5a_judge_run.jsonl]
"""
import argparse
import json
from pathlib import Path

VAL = Path("data/processed/validation")


def load(path: Path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec.get("extraction"), dict):
            out[rec["filename"]] = rec["extraction"]  # last wins
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=VAL / "v5a_judge_run.jsonl")
    args = ap.parse_args()

    released = load(VAL / "v5a_released.jsonl")
    claude = load(VAL / "v5a_claude.jsonl")
    glm5 = load(VAL / "v5a_glm5.jsonl")
    manifest = {}
    mpath = VAL / "v5a_sample_manifest.csv"
    if mpath.exists():
        import csv
        for r in csv.DictReader(mpath.open(encoding="utf-8")):
            manifest[r["filename"]] = r["model"]

    n = skipped = 0
    with args.out.open("w", encoding="utf-8") as f:
        for fn, rel in released.items():
            models = [
                {"model": f"RELEASED ({manifest.get(fn, 'unknown')})", "result": rel},
                {"model": "anthropic/claude-sonnet", "result": claude.get(fn)},
                {"model": "google/glm-5", "result": glm5.get(fn)},
            ]
            usable = [m for m in models if isinstance(m["result"], dict)]
            if len(usable) < 2:
                skipped += 1
                continue
            f.write(json.dumps({"filename": fn, "models": usable}) + "\n")
            n += 1
    print(f"released {len(released)} | claude {len(claude)} | glm5 {len(glm5)}")
    print(f"-> {args.out}: {n} permits with >=2 usable extractions "
          f"({skipped} skipped)")


if __name__ == "__main__":
    main()
