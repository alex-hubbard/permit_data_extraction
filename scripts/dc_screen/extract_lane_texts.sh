#!/bin/bash
# Text-extract the new state-lane PDFs into data/interim/dc_generator_text (resumable; rerun after downloads finish).
cd /home/afhubbard/permit_data_extraction
T=data/interim/dc_generator_text
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/tn_dc_permits/pdfs $T --prefix tn_ --workers 3
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/nc_daq_dc_permits/pdfs $T --prefix nc_ --workers 3
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/md_mde_dc_permits/pdfs $T --prefix md_ --workers 3
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/il_epa_dc_permits/pdfs $T --prefix '' --workers 3
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/wa_dc_permits/pdfs $T --prefix wa_ --workers 3
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/ia_dnr_dc_permits/pdfs $T --prefix ia_ --workers 3
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/oh_epa_dc_permits/pdfs $T --prefix oh_ --workers 3
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/wi_dnr_dc_permits/pdfs $T --prefix wi_ --workers 3
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/ms_mdeq_dc_permits/pdfs $T --prefix ms_ --workers 3
python3 scripts/dc_screen/text_extract_new_pdfs.py data/raw/nj_dep_dc_permits/pdfs $T --prefix nj_ --workers 3 2>/dev/null
echo "lane text extraction pass done $(date)"
