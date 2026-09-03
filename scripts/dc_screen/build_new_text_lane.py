#!/usr/bin/env python3
"""Lane CSV of text files in a text dir that are not yet in a union CSV.

Usage: python scripts/dc_screen/build_new_text_lane.py --union data/processed/permit_data_union_v5e.csv \
          --text-dir data/interim/dc_generator_text --prefixes tx_,ms_,oh_ --out data/processed/gcp_batch/lane2_lane.csv
Filename stems already present in the union (any Status) are skipped; empty text files are skipped.
"""
import argparse, csv, os, sys
from pathlib import Path
csv.field_size_limit(sys.maxsize)
ap = argparse.ArgumentParser()
ap.add_argument('--union', required=True); ap.add_argument('--text-dir', required=True)
ap.add_argument('--prefixes', default=''); ap.add_argument('--out', required=True); ap.add_argument('--min-chars', type=int, default=200)
a = ap.parse_args()
existing = set()
with open(a.union, encoding='utf-8', errors='replace', newline='') as f:
    r = csv.reader(f); cols = next(r); fi = cols.index('Filename')
    for row in r:
        if len(row) > fi: existing.add(row[fi])
prefixes = tuple(p for p in a.prefixes.split(',') if p)
rows = []
for p in sorted(Path(a.text_dir).glob('*.txt')):
    if prefixes and not p.name.startswith(prefixes): continue
    if p.stem in existing: continue
    n = p.stat().st_size
    if n < a.min_chars: continue
    rows.append((p.stem, 'lane2', p.stem[:2].upper() if '_' in p.stem[:3] else '', n))
with open(a.out, 'w', newline='') as f:
    w = csv.writer(f); w.writerow(['filename', 'source', 'state', 'text_bytes']); w.writerows(rows)
print(f'{len(rows)} new docs -> {a.out}; total chars {sum(r[3] for r in rows)/1e6:.0f}M')
