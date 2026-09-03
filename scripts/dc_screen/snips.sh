#!/bin/bash
cd /home/afhubbard/permit_data_extraction
SP=data/processed/analysis/dc_screen/work
tr '\n' '\0' < $SP/snip_list.txt | xargs -0 -P 8 -n 50 grep -oiHE '.{0,160}(data[ -]?cent(er|re)s?|datacenter|server farm|hyperscale|co-?location|IT load|server (hall|room|rack)|behind[- ]the[- ]meter|islanded|private use network|microgrid|not be capable of selling|will not (sell|export)|crypto|bitcoin|blockchain|digital asset).{0,160}' > $SP/snips.txt 2>/dev/null
echo "done $(wc -l < $SP/snips.txt)"
