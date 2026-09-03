#!/bin/bash
cd /home/afhubbard/permit_data_extraction
SP=data/processed/analysis/dc_screen/work
s=$(date +%s)
find data/interim/extracted_text data/interim/dc_generator_text -name '*.txt' -print0 | xargs -0 -P 11 -n 200 grep -oiHE -f $SP/combined_pat.txt > $SP/grep_hits.txt 2>/dev/null
echo "done $(wc -l < $SP/grep_hits.txt) lines $(( $(date +%s)-s ))s"
