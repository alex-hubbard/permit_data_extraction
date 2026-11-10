#!/usr/bin/env python3
"""Re-cut the release validation results to the manufacturing subset.

The document-grounded audit and the release-composition panel were run on a
200-permit sample drawn from the whole release. This script joins that sample
to the resolved-NAICS table and reports the same metrics for the permits whose
resolved code is in sector 31-33, so the manuscript can state how much of the
validation evidence comes from manufacturing permits and whether accuracy on
them differs from the full sample.

Inputs:
    data/processed/validation/v5a_sample_manifest.csv   (the 200-permit release sample)
    data/processed/validation/manual_audit/audit_results_fields.csv
    data/processed/validation/manual_audit/audit_results_units.csv
    data/processed/validation/v5a_judge_run_judge.jsonl       (fifth panel)
    data/processed/analysis/naics_final.csv
Output: a JSON summary on stdout and --out (default data/processed/analysis/mfg_subset/validation_mfg_cut.json).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VAL = ROOT / "data/processed/validation"
csv.field_size_limit(sys.maxsize)
MFG = {"31", "32", "33"}
OK_VERDICTS = {"CORRECT", "NULL_OK"}
AGREE = {"full", "content", "superset"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--naics", type=Path, default=ROOT / "data/processed/analysis/naics_final.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "data/processed/analysis/mfg_subset/validation_mfg_cut.json")
    args = ap.parse_args()

    naics = {}
    for r in csv.DictReader(args.naics.open(encoding="utf-8", errors="replace", newline="")):
        if r["naics_final"]:
            naics[r["filename"].strip()] = r["naics_final"]

    def scope_of(fn: str) -> str:
        code = naics.get(fn.strip(), "")
        if not code:
            return "unresolved"
        return "mfg" if code[:2] in MFG else "non-mfg"

    manifest = list(csv.DictReader((VAL / "v5a_sample_manifest.csv").open(encoding="utf-8")))
    scopes = {r["filename"]: scope_of(r["filename"]) for r in manifest}
    out = {"sample_size": len(manifest), "sample_by_scope": dict(Counter(scopes.values()))}

    # --- document-grounded audit, field level ---
    fields = list(csv.DictReader((VAL / "manual_audit/audit_results_fields.csv").open(encoding="utf-8")))
    for scope in ("all", "mfg"):
        sel = [r for r in fields if scope == "all" or scopes.get(r["filename"]) == "mfg"]
        verdicts = Counter(r["verdict"].upper() for r in sel)
        by_field = defaultdict(Counter)
        for r in sel:
            by_field[r["field"]][r["verdict"].upper()] += 1
        acc = {f: round(sum(v for k, v in c.items() if k in OK_VERDICTS) / sum(c.values()), 4)
               for f, c in by_field.items()}
        permits_with_error = len({r["filename"] for r in sel if r["verdict"].upper() not in OK_VERDICTS})
        out[f"audit_fields_{scope}"] = {
            "judgments": len(sel), "verdicts": dict(verdicts),
            "accuracy": round(sum(v for k, v in verdicts.items() if k in OK_VERDICTS) / max(1, len(sel)), 4),
            "permits": len({r["filename"] for r in sel}), "permits_with_field_error": permits_with_error,
            "accuracy_by_field": dict(sorted(acc.items(), key=lambda kv: -kv[1])),
        }
    # --- document-grounded audit, unit level ---
    units = list(csv.DictReader((VAL / "manual_audit/audit_results_units.csv").open(encoding="utf-8")))
    for scope in ("all", "mfg"):
        sel = [r for r in units if scope == "all" or scopes.get(r["filename"]) == "mfg"]
        checked = sum(int(r["checked_units"] or 0) for r in sel)
        verified = sum(int(r["verified"] or 0) for r in sel)
        wrong = sum(int(r["wrong_attrs"] or 0) for r in sel)
        nf = sum(int(r["not_found"] or 0) for r in sel)
        missing = sum(1 for r in sel if int(r["missing_units_found"] or 0) > 0)
        out[f"audit_units_{scope}"] = {
            "permits": len(sel), "checked": checked, "verified": verified,
            "verified_share": round(verified / max(1, checked), 4),
            "wrong_attrs": wrong, "not_found": nf,
            "permits_with_missing_units": missing,
            "permits_with_missing_units_share": round(missing / max(1, len(sel)), 4),
        }
    # --- fifth panel (release-composition panel), content agreement ---
    recs = [json.loads(l) for l in (VAL / "v5a_judge_run_judge.jsonl").open(encoding="utf-8") if l.strip()]
    for scope in ("all", "mfg"):
        sel = [r for r in recs if scope == "all" or scope_of(r["filename"]) == "mfg"]
        fj = Counter(); n_fields = 0
        for r in sel:
            for f, v in (r.get("judge") or {}).get("fields", {}).items():
                verdict = (v or {}).get("verdict", "")
                if verdict in ("insufficient", ""):
                    continue
                n_fields += 1
                fj["agree" if verdict in AGREE else "conflict"] += 1
        uj = Counter(((r.get("judge") or {}).get("units_overall") or {}).get("verdict", "") for r in sel)
        units_agree = sum(v for k, v in uj.items() if k in AGREE)
        out[f"panel5_{scope}"] = {
            "permits": len(sel), "field_judgments": n_fields,
            "field_content_agreement": round(fj["agree"] / max(1, n_fields), 4),
            "field_conflicts": fj["conflict"],
            "unit_list_agreement": round(units_agree / max(1, len(sel)), 4),
            "unit_verdicts": dict(uj),
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
