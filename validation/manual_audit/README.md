# Document-grounded audit (n = 200)

Public files for the audit described in the paper's Technical Validation section.

| File | Contents |
|---|---|
| `PROTOCOL.md` | Instructions given to the auditor: verdict definitions, unit checks, output format |
| `aggregate_audit.py` | Turns the per-batch audit results into the field and unit tables and the summary |
| `audit_summary.md` | Field accuracy and unit verification totals |
| `audit_verdicts_fields.csv` | One row per audited document and field: `Document ID`, field, verdict |
| `audit_verdicts_units.csv` | One row per audited document: released unit count, units checked, verdict counts, recall probe result |

`Document ID` is the identifier used in the published dataset.

The per-document evidence (quoted snippets, document values, examples of missing units)
and the review sheet of flagged permits quote facility names, addresses and permit
numbers. They are withheld, in line with the de-identification of the public release.
