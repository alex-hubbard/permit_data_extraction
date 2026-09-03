import re, pandas as pd, numpy as np
from collections import Counter, defaultdict
SP='data/processed/analysis/dc_screen/work'
G={
 'dc_strict': re.compile(r"^(data[ -]?cent(er|re)s?|datacenters?|server farm|hyperscale|ai (data|campus|training|computing|workload|facility|infrastructure)|server (hall|room|rack)|digital infrastructure|comput(e|ing) (campus|facility)|cloud (campus|computing)|artificial intelligence|information technology (facility|campus|center))$",re.I),
 'btm_strict': re.compile(r"^(behind[- ]the[- ]meter|island(ed)? mode|islanded|not (be )?(inter)?connected to the.*|off[- ]grid|on[- ]?site (power|generation|electricity|electrical generation)|private use network|pun|microgrid|prime power|self[- ]gen|bridge power|bridging power|captive (power|generation)|not be capable of selling|will not (sell|export)|no (grid|utility) (connection|interconnection)|dedicated (power plant|generation))$",re.I),
 'naics': re.compile(r"^(518210|5182|naics.*518|541511|541512|541519|519130|519190|sic.*7374|7374|7379)$",re.I),
 'crypto': re.compile(r"^(crypto|bitcoin|blockchain|digital asset|mining (rig|machine|hardware)|gpus?|high[- ]performance comput.*|hpc)$",re.I),
 'colo': re.compile(r"^(co-?location|uninterruptible power|static transfer switch|pdus?|computer (equipment|room|floor))$",re.I),
}
GENERIC_OPS=re.compile(r"^(amazon|microsoft|google|oracle|facebook|apple inc|switch,? (inc|ltd|llc)|lambda,? inc|hyperion|tract capital|blackstone|landmark|marathon|aligned|vantage|compass|prometheus hyperscale|nvidia|salesforce|linkedin|dropbox|cloudflare|akamai|zayo|windstream|centurylink|level 3 communications|iron mountain|american tower|lumen technologies|cogent communications|verizon data|1547 critical|ubiquity|edgecore|digital fortress|expedient|involta|lightedge|netrality|peak 10|sungard|us signal|vxchnge|h5 data|infomart|colovore|scala data|edge centres|edgepresence|servercentral|365 data|datasite|internap|softlayer|rackspace|supermicro|foundry digital|hive digital|northern data|argo blockchain|sphere 3d|cathedra|lm funding|bit digital|us bitcoin|abundant mines|texas blockchain|bitnile|ault alliance|dft|carter validus|nautilus data|sentinel data|dupont fabros|ragingwire|quality technology services|cyrus one|core weave|greenidge|stronghold digital|whinstone|rhodium|compute north|gryphon digital|cleanspark|digihost|american bitcoin|hyperscale|qts|edgeconnex|cyrusone)$",re.I)
line_rx=re.compile(r'^data/interim/(extracted_text|dc_generator_text)/(.*?)\.txt:(.*)$')
cnt=defaultdict(Counter); terms=defaultdict(lambda: defaultdict(Counter)); dirs={}
cache={}
def classify(key):
    for k,rx in G.items():
        if rx.match(key): return k
    if key.startswith('naics') or key.startswith('sic'): return 'naics'
    if key in ('it load','it equipment'): return 'itload'
    return 'ops'
with open(f'{SP}/grep_hits.txt',errors='ignore') as f:
    for line in f:
        m=line_rx.match(line.rstrip('\n'))
        if not m: continue
        dr,doc,mt=m.groups(); dirs[doc]=dr
        key=re.sub(r'\s+',' ',mt.lower().strip())
        g=cache.get(key)
        if g is None: g=classify(key); cache[key]=g
        cnt[doc][g]+=1; terms[doc][g][key]+=1
order=['dc_strict','btm_strict','naics','crypto','colo','ops','itload']
rows=[]
for doc,c in cnt.items():
    r={'doc':doc,'dir':dirs[doc]}
    for k in order: r[k]=c.get(k,0)
    for k in ['dc_strict','btm_strict','naics','crypto','ops']:
        r[k+'_terms']='; '.join(f'{t}({n})' for t,n in terms[doc][k].most_common(8))
    rows.append(r)
t=pd.DataFrame(rows).set_index('doc')
print('docs',len(t)); print((t[order]>0).sum().to_dict())
print('dc_strict>=1',(t.dc_strict>=1).sum(),'>=3',(t.dc_strict>=3).sum(),'>=10',(t.dc_strict>=10).sum())
t.to_parquet(f'{SP}/text_hits2.parquet')
# curated
cur=defaultdict(Counter)
with open(f'{SP}/grep_curated.txt',errors='ignore') as f:
    for line in f:
        m=line_rx.match(line.rstrip('\n'))
        if not m: continue
        dr,doc,mt=m.groups(); cur[doc][mt.strip()]+=1
GENERIC_CUR=re.compile(r"^(AI|GW|UPS|chiller|GPU|artificial intelligence|colocation|co-location|hyperscale|microgrid|prime power|IT load|data hall|server hall|fuel cell|liquid cool.*|immersion cool.*|crypto|bitcoin|blockchain|digital asset|mining .*|BTM|behind.the.meter|private use network|islanded|gigawatt|nvidia|hopper|blackwell|H100|B200|compute .*|microsoft|google|alphabet|facebook|meta platforms|amazon data|vadata|oracle .*|openai|anthropic|hubbard|helios|matador|rainier|river bend|design llc|jetstream|lodestar|el paso electric|entergy louisiana|softbank|intersect power|bloom energy|solid oxide fuel cell|proenergy|voltagrid|tallgrass|sailion|poolside|kilby|socrates|hyperion|colossus|stargate|lancium|crusoe|fermi america|laidley|franklin farms|vantage frontier|shackelford|project jupiter|borderplex|dona ana|sunland park|sidecat|fairwater|mount pleasant|port washington|new carlisle|gw ranch|pacifico gw|cipher mining|black pearl|barber lake|applied digital|polaris forge|ellendale|core scientific|terawulf|lake mariner|hut 8|galaxy digital|riot platforms|corsicana|whinstone|sandow lakes|wyoming hyperscale|prometheus hyperscale|nebius|coreweave|fluidstack|sb energy|related digital|saline township|lordstown|gable corp|raven northbrook|greater kansas|southaven|solaris .*|xai|x\.ai corp)$",re.I)
crow=[]
for doc,c in cur.items():
    spec={k:v for k,v in c.items() if not re.match(r"^(AI|GW|UPS|chiller|GPU|artificial intelligence|colocation|co-location|hyperscale|microgrid|prime power|IT load|fuel cell|liquid cool.*|immersion cool.*|crypto|bitcoin|blockchain|digital asset|mining .*|BTM|behind.the.meter|private use network|islanded|gigawatt|nvidia|hopper|blackwell|H100|B200|compute .*|microsoft|google|alphabet|facebook|meta platforms|amazon data|vadata|oracle .*|openai|anthropic|hubbard|helios|matador|rainier|river bend|design llc|jetstream|lodestar|el paso electric|entergy louisiana|softbank|intersect power|bloom energy|solid oxide fuel cell|proenergy|voltagrid|tallgrass|data hall|server hall)$",k,re.I)}
    crow.append({'doc':doc,'cur_all':'; '.join(f'{k}({v})' for k,v in c.most_common(10)),'cur_specific':'; '.join(f'{k}({v})' for k,v in sorted(spec.items(),key=lambda x:-x[1])),'cur_btm':c.get('behind-the-meter',0)+c.get('behind the meter',0)+c.get('BTM',0)+c.get('private use network',0)+c.get('islanded',0),'cur_ai':c.get('AI',0)+c.get('artificial intelligence',0)+c.get('GPU',0)+c.get('nvidia',0),'cur_crypto':sum(v for k,v in c.items() if re.match(r'crypto|bitcoin|blockchain|digital asset|mining',k,re.I))})
ct=pd.DataFrame(crow).set_index('doc')
ct.to_parquet(f'{SP}/curated_hits.parquet')
d=pd.read_parquet(f'{SP}/doc_table2.parquet')
j=d.join(t.drop(columns=['dir']).add_prefix('tx_'),how='left').join(ct,how='left')
for k in order: j['tx_'+k]=j['tx_'+k].fillna(0)
j.to_parquet(f'{SP}/doc_table4.parquet')
pd.set_option('display.width',250); pd.set_option('display.max_colwidth',70); pd.set_option('display.max_rows',300)
strong=(j.tx_dc_strict>=2)|((j.tx_dc_strict>=1)&((j.tx_naics>=1)|(j.tx_btm_strict>=1)|(j.gen_units>=3)))
print('union docs strong',strong.sum(),'site-matched',(strong&j.site_match.notna()).sum(),'unmatched',(strong&j.site_match.isna()).sum())
c=j[strong&j.site_match.isna()].copy()
c['score']=c.tx_dc_strict+3*c.tx_naics+2*c.tx_btm_strict.clip(0,5)+c.tx_crypto.clip(0,3)
c.sort_values('score',ascending=False).to_csv(f'{SP}/union_text_candidates2.csv')
print(c.state.value_counts().head(30).to_dict())
print(c.sort_values('score',ascending=False)[['state','facility','city','gen_units','emerg_units','turb_units','max_unit_mw','issued','tx_dc_strict','tx_btm_strict','tx_naics','tx_dc_strict_terms']].head(120).to_string())
print('--- curated specific hits among union docs (unmatched to sites):')
cs=j[j.cur_specific.fillna('').str.len()>0]
print('docs with specific curated hits',len(cs),'unmatched',cs.site_match.isna().sum())
print(cs[cs.site_match.isna()].sort_values('gen_units',ascending=False)[['state','facility','city','gen_units','turb_units','max_unit_mw','issued','cur_specific']].head(100).to_string())
notin=t[~t.index.isin(d.index)]
notin=notin.join(ct,how='left')
notin[(notin.dc_strict>=2)].sort_values('dc_strict',ascending=False).to_csv(f'{SP}/corpus_not_in_union_dc2.csv')
print('corpus docs not in union with dc_strict>=2:',(notin.dc_strict>=2).sum())
