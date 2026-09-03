#!/bin/bash
# Deterministic NAICS resolution (permit / ECHO / derived / SIC) for the v5e union; LLM stage not run.
set -e; cd /home/afhubbard/permit_data_extraction; export PYTHONPATH=.
python scripts/backfill_naics.py --union data/processed/permit_data_union_v5e.csv
python scripts/build_naics_final.py
python scripts/add_resolved_naics_to_union.py --union data/processed/permit_data_union_v5e.csv --out data/processed/permit_data_union_v5e_naics.csv --refresh
mv data/processed/permit_data_union_v5e_naics.csv data/processed/permit_data_union_v5e.csv
echo "NAICS refresh done $(date)"
