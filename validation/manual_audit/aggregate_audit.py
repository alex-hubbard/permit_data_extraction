"""Aggregate per-batch audit JSONL into field/unit CSVs and a summary.

Usage:
    python validation/manual_audit/aggregate_audit.py [AUDIT_DIR]

AUDIT_DIR holds the per-batch results (``results/batch_*.jsonl``) and receives
the outputs; it defaults to ``data/processed/validation/manual_audit``. That
folder is not published because the results quote facility names, addresses
and permit numbers (see README.md here).
"""
import sys
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUD = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "data/processed/validation/manual_audit"
RESULTS = AUD / "results"

FIELDS = [
    "Facility Name", "Facility Address", "Facility City",
    "Facility State Abbreviation", "Facility Zip Code", "Permit Number",
    "Issuance Date", "Expiration Date", "Regulatory Authority", "NAICS Code",
]
ERROR_VERDICTS = {"INCORRECT", "UNSUPPORTED", "MISSED"}


def main():
    rows = []
    for f in sorted(RESULTS.glob("batch_*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"WARNING: bad JSON line in {f.name}")
                continue
            rec["_batch"] = f.stem
            rows.append(rec)
    seen = Counter(r["filename"] for r in rows)
    dupes = {k: v for k, v in seen.items() if v > 1}
    if dupes:
        print(f"WARNING: duplicate permits: {dupes}")
    print(f"permits audited: {len(seen)}")

    # ---- field-level CSV ----
    with (AUD / "audit_results_fields.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["filename", "batch", "field", "verdict", "doc_value", "evidence"])
        for r in rows:
            for field in FIELDS:
                v = (r.get("fields") or {}).get(field) or {}
                w.writerow([r["filename"], r["_batch"], field,
                            v.get("verdict", "MISSING_VERDICT"),
                            v.get("doc_value", ""), v.get("evidence", "")])

    # ---- unit-level CSV ----
    with (AUD / "audit_results_units.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["filename", "batch", "released_unit_count", "checked_units",
                    "verified", "wrong_attrs", "not_found",
                    "missing_units_found", "recall_complete", "missing_examples"])
        for r in rows:
            u = r.get("units") or {}
            checked = u.get("checked") or []
            vc = Counter(c.get("verdict") for c in checked)
            missing = u.get("missing_units") or []
            w.writerow([r["filename"], r["_batch"], u.get("released_unit_count"),
                        len(checked), vc.get("VERIFIED", 0),
                        vc.get("WRONG_ATTRS", 0), vc.get("NOT_FOUND", 0),
                        len(missing), u.get("recall_complete"),
                        "; ".join(str(m) for m in missing[:5])])

    # ---- summary ----
    field_verdicts = defaultdict(Counter)
    for r in rows:
        for field in FIELDS:
            v = (r.get("fields") or {}).get(field) or {}
            field_verdicts[field][v.get("verdict", "MISSING_VERDICT")] += 1

    unit_tot = Counter()
    recall_gap_permits = 0
    for r in rows:
        u = r.get("units") or {}
        for c in u.get("checked") or []:
            unit_tot[c.get("verdict")] += 1
        if u.get("missing_units"):
            recall_gap_permits += 1

    lines = ["# v5a release audit — summary", "",
             f"Permits audited: {len(seen)} "
             f"(evidence-based LLM audit, see PROTOCOL.md)", "",
             "## General fields", "",
             "| Field | n | Correct | Null-OK | Incorrect | Unsupported | Missed | Accuracy* |",
             "|---|---|---|---|---|---|---|---|"]
    for field in FIELDS:
        c = field_verdicts[field]
        n = sum(c.values())
        correct = c["CORRECT"] + c["NULL_OK"]
        errs = sum(c[v] for v in ERROR_VERDICTS)
        denom = correct + errs
        acc = correct / denom if denom else 0
        lines.append(f"| {field} | {n} | {c['CORRECT']} | {c['NULL_OK']} | "
                     f"{c['INCORRECT']} | {c['UNSUPPORTED']} | {c['MISSED']} | "
                     f"{acc:.1%} |")
    total = Counter()
    for c in field_verdicts.values():
        total.update(c)
    correct = total["CORRECT"] + total["NULL_OK"]
    errs = sum(total[v] for v in ERROR_VERDICTS)
    lines += ["",
              f"**Overall field accuracy: {correct}/{correct+errs} = "
              f"{correct/(correct+errs):.1%}** "
              f"(incorrect {total['INCORRECT']}, unsupported {total['UNSUPPORTED']}, "
              f"missed {total['MISSED']})", "",
              "\\*Accuracy = (CORRECT + NULL_OK) / (those + errors); "
              "MISSING_VERDICT excluded.", "",
              "## Units", "",
              f"Checked units: {sum(unit_tot.values())} — "
              f"VERIFIED {unit_tot['VERIFIED']}, "
              f"WRONG_ATTRS {unit_tot['WRONG_ATTRS']}, "
              f"NOT_FOUND {unit_tot['NOT_FOUND']} "
              f"({unit_tot['VERIFIED']/max(1,sum(unit_tot.values())):.1%} verified)",
              "",
              f"Permits where the recall probe found units missing from the "
              f"release: {recall_gap_permits}/{len(seen)}"]
    (AUD / "audit_summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
