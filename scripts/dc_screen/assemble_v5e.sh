#!/bin/bash
# Union v5e = v5d + recovery-batch rows (12.5k lost/never-extracted docs; the 9 VA
# ERROR-field permits are replaced) + lane-2 rows (TCEQ application/agency-review
# docs + Mississippi). Streaming, constant memory.
set -e
cd /home/afhubbard/permit_data_extraction
export PYTHONPATH=.
G=data/processed/gcp_batch
python scripts/append_new_files_to_union.py --union-in data/processed/permit_data_union_v5d.csv \
  --union-out data/processed/permit_data_union_v5e_tmp.csv --rows-csv $G/recovery_results_rows.csv \
  --source-run gcp_recovery_2026-09 --processing-date 2026-09-02 --replace-files @$G/recovery_replace_files.txt
python scripts/append_new_files_to_union.py --union-in data/processed/permit_data_union_v5e_tmp.csv \
  --union-out data/processed/permit_data_union_v5e.csv --rows-csv $G/lane2_results_rows.csv \
  --source-run gcp_dclane2_2026-09 --processing-date 2026-09-02
rm -f data/processed/permit_data_union_v5e_tmp.csv
echo "v5e written: $(wc -l < data/processed/permit_data_union_v5e.csv) lines"
