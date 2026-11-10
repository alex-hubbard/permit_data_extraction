# v5a release audit — summary

Permits audited: 200 (evidence-based LLM audit, see PROTOCOL.md)

## General fields

| Field | n | Correct | Null-OK | Incorrect | Unsupported | Missed | Accuracy* |
|---|---|---|---|---|---|---|---|
| Facility Name | 200 | 200 | 0 | 0 | 0 | 0 | 100.0% |
| Facility Address | 200 | 167 | 29 | 1 | 0 | 3 | 98.0% |
| Facility City | 200 | 183 | 12 | 2 | 2 | 1 | 97.5% |
| Facility State Abbreviation | 200 | 195 | 4 | 0 | 0 | 1 | 99.5% |
| Facility Zip Code | 200 | 150 | 45 | 1 | 2 | 2 | 97.5% |
| Permit Number | 200 | 185 | 3 | 2 | 0 | 10 | 94.0% |
| Issuance Date | 200 | 115 | 73 | 5 | 5 | 2 | 94.0% |
| Expiration Date | 200 | 127 | 64 | 3 | 2 | 4 | 95.5% |
| Regulatory Authority | 200 | 193 | 1 | 1 | 0 | 5 | 97.0% |
| NAICS Code | 200 | 40 | 152 | 4 | 4 | 0 | 96.0% |

**Overall field accuracy: 1938/2000 = 96.9%** (incorrect 19, unsupported 15, missed 28)

\*Accuracy = (CORRECT + NULL_OK) / (those + errors); MISSING_VERDICT excluded.

## Units

Checked units: 790 — VERIFIED 764, WRONG_ATTRS 24, NOT_FOUND 2 (96.7% verified)

Permits where the recall probe found units missing from the release: 39/200
