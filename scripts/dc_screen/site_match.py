import pandas as pd, numpy as np, re
SP='data/processed/analysis/dc_screen/work'
doc=pd.read_parquet(f'{SP}/doc_table.parquet')
s=pd.read_csv('/home/afhubbard/permit_data_extraction/data/processed/analysis/dc_generation_sites.csv')
STOP=set('llc inc corp corporation company co lp ltd the of and data center centers datacenter campus facility site services service llp plc l.l.c l.p group holdings properties property partners ventures llc. inc. dba a an at in for environmental'.split())
def toks(x):
    x=re.sub(r'[^a-z0-9 ]',' ',str(x).lower())
    return {t for t in x.split() if t not in STOP and len(t)>=3 and not t.isdigit()}
def city_n(x):
    x=re.sub(r'[^a-z ]','',str(x).lower()).strip()
    return re.sub(r'\s+',' ',x)
def addr_key(x):
    m=re.match(r'\s*(\d+)\s+([a-z0-9]+)', str(x).lower())
    return f'{m.group(1)} {m.group(2)}' if m else None
def permits(x):
    return {p.strip().lower() for p in re.split(r'[;,/]| and ', str(x)) if len(p.strip())>=4}
s['stoks']=(s.facility_name.fillna('')+' '+s.owner_operator.fillna('')).map(toks)
s['scity']=s.city.map(city_n); s['saddr']=s.address.map(addr_key); s['sperm']=s.permit_numbers.map(permits)
s['sdocs']=s.source_files.fillna('').map(lambda v:{d.strip() for d in v.split(';')})
doc['_toks']=(doc.facility.fillna('')+' '+doc.owner.fillna('')).map(toks)
doc['_city']=doc.city.map(city_n); doc['_addr']=doc.address.map(addr_key); doc['_perm']=doc.permit.map(permits)
SPEC=r"amazon|vadata|microsoft|google|alphabet|design llc|meta platforms|facebook|oracle|equinix|digital realty|cyrusone|cyrus one|\bqts\b|quality technology|quality investment|vantage|aligned|compass data|edgeconnex|cloudhq|databank|flexential|novva|sabey|t5 |prime data|cologix|coresite|iron mountain|ntt |stack infra|skybox|stream data|crusoe|coreweave|lambda|\bxai\b|x\.ai|openai|tract|rowan digital|powerhouse|corscale|yondr|ada infra|datavault|core scientific|applied digital|cipher|riot |marathon digital|terawulf|hut 8|galaxy digital|\biren\b|iris energy|bitdeer|sailion|poolside|fermi|nebius|lancium|stargate|colossus|hyperion|prometheus|nscale|related digital|soluna|bitfarms|cleanspark|mawson|cormint|compute north|hyperscale|voltagrid|fluidstack|anthropic|sandow|kilby|nexus|longhorn|gw ranch|pacifico|tierpoint|centersquare|cyxtera|lumen|cogent|mapletree|element critical|evoque|edged|nvidia|kyndryl|\bibm\b|salesforce|linkedin|dropbox|rackspace|cloudflare|akamai|zayo|windstream|centurylink|level 3|internap|servercentral|365 data|datasite|expedient|involta|lightedge|netrality|peak 10|sungard|us signal|vxchnge|digital fortress|h5 data|infomart|colovore|scala|edgecore|\baws\b|ragingwire|nautilus|dupont fabros|sentinel data|1547|ubiquity|bitmain|greenidge|stronghold|whinstone|rhodium|gryphon|bit digital|northern data|digihost|american bitcoin|bitnile|data ?cent|datacenter|server farm|colocation|hosting|tapaha|switch"
by_state={st:list(g.itertuples()) for st,g in s.groupby('state')}
def match(fn,row):
    g=by_state.get(row.state)
    if g is None: return None,None
    best=None
    for r in g:
        how=None
        if fn in r.sdocs: how='doc'
        elif row._addr and row._addr==r.saddr: how='addr'
        elif row._perm and (row._perm & r.sperm): how='permit'
        elif row._city and row._city==r.scity and len(row._toks & r.stoks)>=1: how='city+name'
        if how:
            rank={'doc':0,'addr':1,'permit':2,'city+name':3}[how]
            if best is None or rank<best[0]: best=(rank,how,r.facility_name,r.evidence_tier)
    if best: return best[1], f'{best[2]} [{best[3]}]'
    return None,None
nm0=(doc.facility.fillna('')+' | '+doc.owner.fillna('')).str.lower()
doc['big_fleet']=(doc.emerg_units>=8)&(doc.max_unit_mw>=1)
pre=doc.naics_dc|doc.name_dc_word|nm0.str.contains(SPEC,regex=True)|doc.big_fleet|doc.in_sites.notna()|(doc.turb_units>=1)|(doc.gen_units>=5)
print('docs to match',pre.sum())
res=[match(fn,row) if pre[fn] else (None,None) for fn,row in doc.iterrows()]
doc['site_match_how']=[r[0] for r in res]; doc['site_match']=[r[1] for r in res]
print(doc.site_match_how.value_counts(dropna=False))
# specific operator hits (not generic words)
nm=(doc.facility.fillna('')+' | '+doc.owner.fillna('')).str.lower()
doc['spec_hit']=nm.str.extract('('+SPEC+')')[0]
doc['big_fleet']=(doc.emerg_units>=8)&(doc.max_unit_mw>=1)
doc['sig_any']=doc.naics_dc|doc.name_dc_word|doc.spec_hit.notna()|doc.big_fleet
doc.drop(columns=['_toks','_perm']).to_parquet(f'{SP}/doc_table2.parquet')
cand=doc[doc.sig_any & doc.site_match.isna()].copy()
cand['score']=cand.naics_dc.astype(int)*3+cand.name_dc_word.astype(int)*3+cand.spec_hit.notna().astype(int)*2+cand.big_fleet.astype(int)*2
print('candidates', len(cand)); print(cand.groupby(['naics_dc','name_dc_word']).size())
cols=['state','facility','owner','city','naics','spec_hit','gen_units','emerg_units','turb_units','max_unit_mw','issued','source_run','score']
pd.set_option('display.width',250); pd.set_option('display.max_colwidth',45)
print('--- NAICS or name says DC, unmatched:')
print(cand[cand.naics_dc|cand.name_dc_word].sort_values(['state','gen_units'],ascending=[True,False])[cols].to_string())
print('--- operator hit only (no naics/name-dc), unmatched, with >=1 gen unit:')
x=cand[~(cand.naics_dc|cand.name_dc_word)&cand.spec_hit.notna()&(cand.gen_units>=1)]
print(x.spec_hit.value_counts().head(40))
print(x.sort_values('emerg_units',ascending=False)[cols].head(80).to_string())
# --- known operators for site-level check
print('--- checks:'); 
for pat in ['cmh116','tapaha','kyndryl','national security','vantage data center tx2','sparks','78-01-121','ohio|\\bOH\\b']:
    m=s[s.facility_name.str.contains(pat,case=False,na=False)|s.owner_operator.str.contains(pat,case=False,na=False)|s.city.str.contains(pat,case=False,na=False)]
    print(pat, len(m), m.facility_name.head(5).tolist())
