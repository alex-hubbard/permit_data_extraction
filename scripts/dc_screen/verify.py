import re, json, pandas as pd, numpy as np
from collections import defaultdict, Counter
SP='data/processed/analysis/dc_screen/work'
j=pd.read_parquet(f'{SP}/doc_table4.parquet')
th=pd.read_parquet(f'{SP}/text_hits2.parquet')
# --- snippets
line_rx=re.compile(r'^data/interim/(extracted_text|dc_generator_text)/(.*?)\.txt:(.*)$')
sn=defaultdict(list)
with open(f'{SP}/snips.txt',errors='ignore') as f:
    for line in f:
        m=line_rx.match(line.rstrip('\n'))
        if m: sn[m.group(2)].append(m.group(3))
TIED=re.compile(r"(proposed|new|the|this|a|an|its|their|planned|existing|adjacent|on-site|onsite|hyperscale|colocation|co-location|enterprise|commercial|large|mission[- ]critical|ai|cloud)\s+(data ?cent(er|re)s?|datacenters?)|data ?cent(er|re)s?\s+(campus|facility|facilities|project|building|buildings|operations?|will|would|is|are|shall|located|consist|that|which|at|in|owned|operated|to be|and its|site|complex|use|load|tenant|customer|construction|development|phase|expansion|market)|(serve|serving|support|supporting|power|powering|for|at)\s+(the|a|an|its|this)?\s*(data ?cent(er|re)|datacenter)|behind[- ]the[- ]meter|islanded|private use network|not be capable of selling|will not (sell|export)|server (hall|room|rack)|IT load|hyperscale|colocation|co-location|crypto|bitcoin|blockchain|digital asset|mining", re.I)
BOILER=re.compile(r"national climatic data center|climatic data|data center exchange|central data (exchange|center)|epa.{0,20}data center|air quality data center|such as data centers|(e\.g\.|including|like|for example),?\s*(hospitals|data centers)|data centers,? (hospitals|telecommunications|and other)|(hospitals|schools|nursing homes|telecommunications facilities),? (and )?data centers|nsps|neshap|subpart|source category|regulatory (text|definition)|state implementation|general permit|rule 1110|boilerplate|typical (of|for) data centers|areas such as data centers|applications (such as|including) data centers|industries (such as|including) data centers|emergency generators (at|for) (hospitals|data centers)|www\.|\.gov", re.I)
def ctx(doc):
    s=sn.get(doc,[])
    t=sum(1 for x in s if TIED.search(x)); b=sum(1 for x in s if BOILER.search(x) and not TIED.search(x))
    return t,b,len(s)
res=[ctx(d) for d in j.index]
j['ctx_tied']=[r[0] for r in res]; j['ctx_boiler']=[r[1] for r in res]; j['ctx_n']=[r[2] for r in res]
j.to_parquet(f'{SP}/doc_table5.parquet')
strong=(j.tx_dc_strict>=2)|((j.tx_dc_strict>=1)&((j.tx_naics>=1)|(j.tx_btm_strict>=1)|(j.gen_units>=3)))|j.naics_dc|j.name_dc_word|j.spec_hit.isin(['novva','cyrusone','databank','aligned','vadata','microsoft','powerhouse','flexential','iren','tapaha','data cent','datacenter','colocation','hosting','equinix','digital realty','qts','quality investment','vantage','compass data','edgeconnex','cloudhq','sabey','stream data','crusoe','coreweave','core scientific','applied digital','cipher','galaxy digital','terawulf','hut 8','bitdeer','poolside','fermi','lancium','stargate','nscale','soluna','cleanspark','fluidstack','anthropic','openai','kilby','nexus','longhorn','gw ranch','tierpoint','cyxtera','centersquare','element critical','evoque','kyndryl','ibm','cologix','coresite','iron mountain','ntt ','stack infra','skybox','t5 ','prime data','switch','amazon','google','facebook','meta platforms','oracle','apple'])
c=j[strong & j.site_match.isna()].copy()
c['ctx_score']=c.ctx_tied-0.5*c.ctx_boiler
c['sig']=(c.naics_dc.astype(int)*3+c.name_dc_word.astype(int)*3+c.spec_hit.notna().astype(int)*1+c.big_fleet.astype(int)*2+(c.tx_dc_strict>=2).astype(int)*2+(c.tx_naics>=1).astype(int)*2+(c.tx_btm_strict>=1).astype(int)+c.ctx_tied.clip(0,5))
c=c.sort_values(['sig','gen_units'],ascending=False)
c.to_csv(f'{SP}/union_candidates_all.csv')
print('union unmatched candidates (any signal):',len(c),' with gen_units>0:',(c.gen_units>0).sum(),' ctx_tied>=1:',(c.ctx_tied>=1).sum())
pd.set_option('display.width',250); pd.set_option('display.max_colwidth',50); pd.set_option('display.max_rows',300)
cols=['state','facility','city','gen_units','emerg_units','turb_units','max_unit_mw','issued','sig','ctx_tied','ctx_boiler','spec_hit']
top=c[(c.ctx_tied>=1)|c.naics_dc|c.name_dc_word|(c.spec_hit.notna()&(c.gen_units>0))]
print('--- candidates with project-tied context or DC name/NAICS (n=%d):'%len(top))
print(top[cols].head(140).to_string())
with open(f'{SP}/cand_snippets.txt','w') as f:
    for d,r in top.iterrows():
        f.write(f"\n=== {d} | {r.state} | {r.facility} | {r.city} | gen={r.gen_units} tied={r.ctx_tied} boiler={r.ctx_boiler}\n")
        ss=[x for x in sn.get(d,[]) if TIED.search(x)][:3] or sn.get(d,[])[:2]
        for x in ss: f.write('   > '+x[:330]+'\n')
# --- lane audit: DC lane + VA DC docs not matched
lane=j[(j.source_run=='dc_generators_2026-08')|j.index.str.contains('_DC_Permit')]
print('\n--- lane audit: docs',len(lane),'site-matched',lane.site_match.notna().sum(),'unmatched w/ gen units',((lane.site_match.isna())&(lane.gen_units>0)).sum(),'unmatched no units',((lane.site_match.isna())&(lane.gen_units==0)).sum())
tx=j[j.index.str.startswith('tx_')&(j.source_run=='dc_generators_2026-08')].copy()
tx['dtype']=tx.index.str.extract(r'_(Technical_Review|Initial_Application|Final_Action|[A-Za-z_]+)$',expand=False)
print(tx.groupby('dtype').agg(n=('gen_units','size'),zero_units=('gen_units',lambda x:(x==0).sum()),matched=('site_match',lambda x:x.notna().sum())).to_string())
# --- corpus docs not in union
d=j.index
notin=th[~th.index.isin(d)].copy()
ct=pd.read_parquet(f'{SP}/curated_hits.parquet')
notin=notin.join(ct,how='left')
notin['ctx_tied']=[ctx(x)[0] for x in notin.index]; notin['ctx_boiler']=[ctx(x)[1] for x in notin.index]
log={}
with open('/home/afhubbard/permit_data_extraction/data/interim/processed_files_log.jsonl') as f:
    for line in f:
        try: r=json.loads(line)
        except: continue
        log[r['filename'].rsplit('.txt',1)[0]]=r.get('status')
notin['log_status']=[log.get(x) for x in notin.index]
nn=notin[(notin.dc_strict>=2)|((notin.dc_strict>=1)&(notin.ctx_tied>=1))].sort_values(['ctx_tied','dc_strict'],ascending=False)
nn.to_csv(f'{SP}/corpus_not_in_union_final.csv')
print('\n--- corpus docs NOT in union with DC text (n=%d); log status:'%len(nn), nn.log_status.value_counts(dropna=False).to_dict())
print(nn[['dir','dc_strict','btm_strict','naics','ctx_tied','ctx_boiler','log_status','dc_strict_terms']].head(60).to_string())
# --- known-project recall
KEY=['colossus','xai','stargate','lancium','crusoe','socrates','fairwater','rainier','hyperion','laidley','franklin farms','sidecat','gable corp','vantage frontier','shackelford','project jupiter','borderplex','sailion','poolside','tallgrass','voltagrid','proenergy','bloom energy','nebius','coreweave','fluidstack','related digital','sb energy','cipher mining','applied digital','core scientific','terawulf','hut 8','galaxy digital','riot platforms','whinstone','greenidge','fermi america','kilby','gw ranch','pacifico gw','sandow lakes','wyoming hyperscale','prometheus hyperscale','southaven','solaris','openai','anthropic','oracle america','oracle cloud','intersect power','new carlisle','port washington','mount pleasant','lordstown','saline township','black pearl','barber lake','polaris forge','ellendale','lake mariner','river bend','corsicana']
allc=ct.copy()
rows=[]
for k in KEY:
    hit=allc[allc.cur_all.str.contains(re.escape(k),case=False,na=False)]
    inu=[x for x in hit.index if x in j.index]
    matched=[x for x in inu if pd.notna(j.loc[x,'site_match'])]
    rows.append((k,len(hit),len(inu),len(matched),'; '.join(list(hit.index)[:3])))
print('\n--- known-project name recall (docs in corpus / in union / site-matched):')
for r in rows: print(f'{r[0]:22s} corpus={r[1]:4d} union={r[2]:4d} sites={r[3]:4d}  e.g. {r[4][:150]}')
