#!/bin/bash
# Lane 3 = the new state-lane documents (TN/NC/MD/IL/WA/IA/OH/WI/NJ). Waits for extract_lane_texts.sh to finish,
# extends the text screen to the new documents, builds the lane, runs the Vertex batch, appends the rows to the
# union as v5f, and rebuilds the site table. Rerunnable: each step skips what already exists.
cd /home/afhubbard/permit_data_extraction; export PYTHONPATH=.
W=data/processed/analysis/dc_screen/work; G=data/processed/gcp_batch; T=data/interim/dc_generator_text
until grep -q "lane text extraction pass done" data/interim/lane_text_extraction.log; do sleep 60; done
echo "[$(date)] lane texts ready"
python scripts/dc_screen/build_new_text_lane.py --union data/processed/permit_data_union_v5e.csv --text-dir $T \
  --prefixes tn_,nc_,md_,il_,wa_,ia_,oh_,wi_,nj_,ms_,tx_ --out $G/lane3_lane.csv
cut -d, -f1 $G/lane3_lane.csv | tail -n +2 | sed "s|^|$T/|; s|$|.txt|" > $W/new_docs_lane3.txt
tr '\n' '\0' < $W/new_docs_lane3.txt | xargs -0 -P 4 -n 50 grep -oiHE -f scripts/dc_screen/combined_pat.txt >> $W/grep_hits.txt 2>/dev/null
tr '\n' '\0' < $W/new_docs_lane3.txt | xargs -0 -P 4 -n 50 grep -oiHE '.{0,160}(data[ -]?cent(er|re)s?|datacenter|server farm|hyperscale|co-?location|IT load|server (hall|room|rack)|behind[- ]the[- ]meter|islanded|private use network|microgrid|not be capable of selling|will not (sell|export)|crypto|bitcoin|blockchain|digital asset).{0,160}' >> $W/snips.txt 2>/dev/null
python scripts/dc_screen/update_text_hits.py $W
TEXT_DIR=$T scripts/dc_screen/run_recovery_batch.sh $G/lane3_lane.csv || { echo "lane3 batch failed"; exit 1; }
python scripts/append_new_files_to_union.py --union-in data/processed/permit_data_union_v5e.csv --union-out data/processed/permit_data_union_v5f.csv \
  --rows-csv $G/lane3_results_rows.csv --source-run gcp_dclane3_2026-09 --processing-date 2026-09-02
python scripts/build_dc_generation_sites.py --union data/processed/permit_data_union_v5f.csv --work-dir $W \
  --prior-sites data/processed/analysis/dc_generation_sites_2026-08-21.csv --out data/processed/analysis/dc_generation_sites.csv
echo "[$(date)] LANE3 PIPELINE DONE -> permit_data_union_v5f.csv + dc_generation_sites.csv"
