#!/usr/bin/env python3
"""Re-aggregate <work>/grep_hits.txt into <work>/text_hits2.parquet (strict term classes) after new
documents have been appended by an incremental grep pass (see extract_lane_texts / lane-3 pipeline)."""
import re, sys, pandas as pd
from collections import Counter, defaultdict
W = sys.argv[1] if len(sys.argv) > 1 else 'data/processed/analysis/dc_screen/work'
G = {
 'dc_strict': re.compile(r"^(data[ -]?cent(er|re)s?|datacenters?|server farm|hyperscale|ai (data|campus|training|computing|workload|facility|infrastructure)|server (hall|room|rack)|digital infrastructure|comput(e|ing) (campus|facility)|cloud (campus|computing)|artificial intelligence|information technology (facility|campus|center))$", re.I),
 'btm_strict': re.compile(r"^(behind[- ]the[- ]meter|island(ed)? mode|islanded|not (be )?(inter)?connected to the.*|off[- ]grid|on[- ]?site (power|generation|electricity|electrical generation)|private use network|pun|microgrid|prime power|self[- ]gen|bridge power|bridging power|captive (power|generation)|not be capable of selling|will not (sell|export)|no (grid|utility) (connection|interconnection)|dedicated (power plant|generation))$", re.I),
 'naics': re.compile(r"^(518210|5182|naics.*518|541511|541512|541519|519130|519190|sic.*7374|7374|7379)$", re.I),
 'crypto': re.compile(r"^(crypto|bitcoin|blockchain|digital asset|mining (rig|machine|hardware)|gpus?|high[- ]performance comput.*|hpc)$", re.I),
 'colo': re.compile(r"^(co-?location|uninterruptible power|static transfer switch|pdus?|computer (equipment|room|floor))$", re.I),
}
line_rx = re.compile(r'^data/interim/(extracted_text|dc_generator_text)/(.*?)\.txt:(.*)$')
cnt = defaultdict(Counter); terms = defaultdict(lambda: defaultdict(Counter)); dirs = {}; cache = {}
def classify(key):
    for k, rx in G.items():
        if rx.match(key): return k
    if key.startswith('naics') or key.startswith('sic'): return 'naics'
    if key in ('it load', 'it equipment'): return 'itload'
    return 'ops'
with open(f'{W}/grep_hits.txt', errors='ignore') as f:
    for line in f:
        m = line_rx.match(line.rstrip('\n'))
        if not m: continue
        dr, doc, mt = m.groups(); dirs[doc] = dr
        key = re.sub(r'\s+', ' ', mt.lower().strip())
        g = cache.get(key)
        if g is None: g = classify(key); cache[key] = g
        cnt[doc][g] += 1; terms[doc][g][key] += 1
order = ['dc_strict', 'btm_strict', 'naics', 'crypto', 'colo', 'ops', 'itload']
rows = []
for doc, c in cnt.items():
    r = {'doc': doc, 'dir': dirs[doc]}
    for k in order: r[k] = c.get(k, 0)
    for k in ['dc_strict', 'btm_strict', 'naics', 'crypto', 'ops']:
        r[k + '_terms'] = '; '.join(f'{t}({n})' for t, n in terms[doc][k].most_common(8))
    rows.append(r)
t = pd.DataFrame(rows).set_index('doc'); t.to_parquet(f'{W}/text_hits2.parquet')
print('docs with hits', len(t), 'dc_strict>=1', int((t.dc_strict >= 1).sum()))
