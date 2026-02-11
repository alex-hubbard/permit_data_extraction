#!/usr/bin/env markdown
# Data Release And Documentation Plan

This document specifies a default release pathway for the dataset and the
documentation expected for a Nature Scientific Data submission.

## Recommended Release Platform

- **Primary:** Zenodo (DOI minting, versioning, GitHub integration).
- **Alternate:** Dataverse (institutional hosting) if required by policy.

## Release Artifacts

1. **Dataset file(s):**
   - `permit_data_extracted.xlsx`
   - Optional: CSV export for easier programmatic use.
2. **Metadata:**
   - Title, authors, affiliations, keywords.
   - Abstract describing coverage and scope.
3. **Documentation:**
   - Data dictionary (see `nature_data_schema.md`).
   - Pipeline overview (see `nature_data_pipeline_mapping.md`).
   - Validation summary (see `nature_data_validation.md`).

## Versioning

- **v0.x:** partial coverage (current states/programs).
- **v1.0:** full US coverage (roadmap completion).
- Increment minor versions for new states or significant processing updates.

## Licensing And Terms

- **Default:** CC-BY 4.0, pending confirmation of agency portal terms.
- Record any state-specific restrictions in release notes if needed.

## Packaging Checklist

- Verify all required fields exist in the output table.
- Confirm no PII beyond public permit disclosures is included.
- Include checksum hashes for each released file.
- Provide a README with usage examples and citation guidance.
