"""Aggregate multi-model validation runs into paper-ready agreement statistics.

Reads all validation_run_*.jsonl files under data/processed/validation/, pools the
per-permit records, and reports:
  - per general field: completeness (>=1 model produced a value), agreement
    (no conflicting values among models that produced one)
  - unit-level agreement ratio distribution
  - flag-reason distribution
Writes validation_summary.json and validation_summary.md next to the inputs.
"""

import json
import statistics
from collections import Counter
from pathlib import Path

VALIDATION_DIR = Path(__file__).resolve().parents[1] / "data" / "processed" / "validation"


def load_runs():
    runs = []
    for path in sorted(VALIDATION_DIR.glob("validation_run_*.jsonl")):
        with open(path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        meta = records[0] if records and records[0].get("_run_meta") else {}
        body = [r for r in records if not r.get("_run_meta")]
        runs.append({"path": path, "meta": meta, "records": body})
    return runs


def main():
    runs = load_runs()

    # De-duplicate within each model panel by permit filename, keeping the
    # record from the most recent run. Runs are loaded in chronological order
    # (timestamped filenames sort chronologically), so a later run overrides an
    # earlier one for the same (panel, permit). This is how a re-validation run
    # supersedes the failed records it was created to replace (e.g. permits lost
    # to an API authorization lapse), which use the same panel. The key includes
    # the panel because the same permit validated by a *different* panel is a
    # separate, legitimate observation and must be kept. Records with no filename
    # are always kept.
    pooled_by_key = {}
    pooled_no_fn = []
    superseded = Counter()
    for run in runs:
        panel = tuple(run["meta"].get("model_names") or [])
        for rec in run["records"]:
            fn = rec.get("filename")
            if not fn:
                pooled_no_fn.append(rec)
                continue
            key = (panel, fn)
            if key in pooled_by_key:
                superseded[pooled_by_key[key]["_run_name"]] += 1
            rec = dict(rec, _run_name=run["path"].name)
            pooled_by_key[key] = rec
    pooled = list(pooled_by_key.values()) + pooled_no_fn

    if superseded:
        print("Superseded records (replaced by a later run): "
              + ", ".join(f"{name}: {c}" for name, c in superseded.items()))

    panels = Counter()
    for run in runs:
        models = tuple(run["meta"].get("model_names") or [])
        kept = sum(1 for r in pooled if r.get("_run_name") == run["path"].name)
        panels[models] += kept

    n = len(pooled)
    statuses = Counter(r.get("status") for r in pooled)
    reasons = Counter((r.get("reason") or "—").split("'")[0].strip() for r in pooled if r.get("status") != "SUCCESS")

    field_stats = {}
    for rec in pooled:
        for field, s in (rec.get("field_summary") or {}).items():
            st = field_stats.setdefault(field, {"present": 0, "agree": 0, "total": 0})
            st["total"] += 1
            if s.get("complete_models"):
                st["present"] += 1
                if not s.get("needs_review"):
                    st["agree"] += 1

    ratios = [r["unit_agreement_ratio"] for r in pooled if isinstance(r.get("unit_agreement_ratio"), (int, float))]

    unit_field_stats = {}
    for rec in pooled:
        for unit_key, fields in (rec.get("unit_summary") or {}).items():
            for field, s in fields.items():
                st = unit_field_stats.setdefault(field, {"agree": 0, "total": 0})
                st["total"] += 1
                if not s.get("needs_review"):
                    st["agree"] += 1

    kept_by_run = Counter(r.get("_run_name") for r in pooled)
    summary = {
        "runs": [
            {
                "path": str(r["path"].name),
                "n": len(r["records"]),
                "n_kept": kept_by_run.get(r["path"].name, 0),
                "models": r["meta"].get("model_names"),
            }
            for r in runs
        ],
        "pooled_permits": n,
        "status_counts": dict(statuses),
        "flag_reasons": dict(reasons),
        "general_field_stats": {
            f: {
                "completeness": round(s["present"] / s["total"], 3) if s["total"] else None,
                "agreement_given_present": round(s["agree"] / s["present"], 3) if s["present"] else None,
            }
            for f, s in field_stats.items()
        },
        "unit_field_agreement": {
            f: {"agreement": round(s["agree"] / s["total"], 3), "n_comparisons": s["total"]}
            for f, s in unit_field_stats.items()
        },
        "unit_agreement_ratio": {
            "n": len(ratios),
            "mean": round(statistics.mean(ratios), 3) if ratios else None,
            "median": round(statistics.median(ratios), 3) if ratios else None,
            "min": round(min(ratios), 3) if ratios else None,
            "share_ge_0.70": round(sum(r >= 0.70 for r in ratios) / len(ratios), 3) if ratios else None,
            "share_eq_1.00": round(sum(r == 1.0 for r in ratios) / len(ratios), 3) if ratios else None,
        },
    }

    out_json = VALIDATION_DIR / "validation_summary.json"
    out_json.write_text(json.dumps(summary, indent=2))

    lines = [
        "# Multi-Model Validation Summary (pooled runs)",
        "",
        f"Pooled permits: **{n}** across {len(runs)} runs.",
        "",
        "| Run | n | n kept | Models |",
        "|---|---|---|---|",
    ]
    for r in summary["runs"]:
        lines.append(f"| {r['path']} | {r['n']} | {r['n_kept']} | {', '.join(r['models'] or [])} |")
    lines += [
        "",
        f"Status: {dict(statuses)}",
        "",
        "## General fields (completeness / agreement when present)",
        "",
        "| Field | Completeness | Agreement |",
        "|---|---|---|",
    ]
    for f_, s in sorted(summary["general_field_stats"].items()):
        lines.append(f"| {f_} | {s['completeness']} | {s['agreement_given_present']} |")
    lines += [
        "",
        "## Unit fields (agreement across units present in >=2 models)",
        "",
        "| Field | Agreement | N comparisons |",
        "|---|---|---|",
    ]
    for f_, s in sorted(summary["unit_field_agreement"].items()):
        lines.append(f"| {f_} | {s['agreement']} | {s['n_comparisons']} |")
    u = summary["unit_agreement_ratio"]
    lines += [
        "",
        "## Unit agreement ratio per permit",
        "",
        f"- n = {u['n']}, mean = {u['mean']}, median = {u['median']}, min = {u['min']}",
        f"- share >= 0.70 threshold: {u['share_ge_0.70']}",
        f"- share fully consistent (1.00): {u['share_eq_1.00']}",
        "",
        "## Flag reasons",
        "",
    ]
    for reason, count in reasons.most_common():
        lines.append(f"- {reason}: {count}")
    (VALIDATION_DIR / "validation_summary.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
