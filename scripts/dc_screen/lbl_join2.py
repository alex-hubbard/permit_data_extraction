import pandas as pd, numpy as np, re
SP='data/processed/analysis/dc_screen/work'
doc=pd.read_parquet(f'{SP}/doc_table.parquet')
m=pd.read_csv('/home/afhubbard/lbl-data-center-map/data/processed/combined_data_centers.csv', low_memory=False)
STOP=set('llc inc corp corporation company co lp ltd the of and data center centers datacenter campus facility site services service llp plc group holdings properties property partners ventures dba trust realty management inc. llc. environmental'.split())
def toks(x):
    x=re.sub(r'[^a-z0-9 ]',' ',str(x).lower())
    return {t for t in x.split() if t not in STOP and len(t)>=3 and not t.isdigit()}
def city_n(x):
    x=re.sub(r'[^a-z ]','',str(x).lower()).strip(); return re.sub(r'\s+',' ',x)
def addr_key(x):
    mm=re.match(r'\s*(\d+)\s+([a-z0-9]+)', str(x).lower()); return f'{mm.group(1)} {mm.group(2)}' if mm else None
# operator canonical tokens
OPTOK={'amazon':{'amazon','vadata','aws'},'microsoft':{'microsoft'},'alphabet':{'google','alphabet','design'},'meta':{'meta','facebook','sidecat','greater'},
 'oracle':{'oracle'},'equinix':{'equinix'},'digital realty':{'digital','dupont','fabros'},'cyrusone':{'cyrusone','cyrus'},'qts':{'qts','quality'},'stack':{'stack'},
 'vantage':{'vantage'},'compass':{'compass'},'aligned':{'aligned'},'edgeconnex':{'edgeconnex'},'cloudhq':{'cloudhq'},'databank':{'databank'},'flexential':{'flexential'},
 'sabey':{'sabey'},'switch':{'switch'},'stream':{'stream'},'coresite':{'coresite'},'iron mountain':{'iron','mountain'},'ntt':{'ntt','ragingwire'},'powerhouse':{'powerhouse'},'tract':{'tract'},'novva':{'novva'},'t5':{'t5'},'cologix':{'cologix'},'tierpoint':{'tierpoint'},'lumen':{'lumen','centurylink','level','qwest'},'cogent':{'cogent'},'crusoe':{'crusoe'},'coreweave':{'coreweave'},'applied digital':{'applied'},'core scientific':{'core','scientific'},'cipher':{'cipher'},'riot':{'riot','whinstone'},'marathon':{'marathon'},'terawulf':{'terawulf'},'hut 8':{'hut'},'galaxy':{'galaxy'},'iren':{'iren','iris'},'bitdeer':{'bitdeer'},'prime':{'prime'},'skybox':{'skybox'},'yondr':{'yondr'},'corscale':{'corscale'},'rowan':{'rowan'},'element critical':{'element'},'evoque':{'evoque'},'centersquare':{'centersquare','cyxtera'},'verizon':{'verizon'},'at&t':{'at&t','att'},'lambda':{'lambda'},'xai':{'xai','x.ai'},'openai':{'openai'},'nebius':{'nebius'},'fermi':{'fermi'},'poolside':{'poolside'},'sailion':{'sailion'},'mapletree':{'mapletree'},'crown castle':{'crown','castle'},'apple':{'apple'},'ibm':{'ibm','kyndryl'},'hpe':{'hewlett','hpe'},'dell':{'dell'},'salesforce':{'salesforce'},'dropbox':{'dropbox'},'linkedin':{'linkedin'},'twitter':{'twitter','x corp'},'visa':{'visa'},'american express':{'american','express'},'wells fargo':{'wells','fargo'},'jpmorgan':{'jpmorgan','chase'},'bank of america':{'bank','america'},'fidelity':{'fidelity'},'state farm':{'state','farm'},'nsa':{'nsa','national','security'},'nasa':{'nasa'},'doe':{'doe'},'ornl':{'oak','ridge'},'lanl':{'los','alamos'}}
def op_tokens(op):
    o=str(op).lower()
    for k,v in OPTOK.items():
        if k in o: return v
    return toks(op)
m['_city']=m.city.map(city_n); m['_addr']=m.address.map(addr_key); m['_optok']=m.operator.map(op_tokens); m['_ftok']=[toks(f)-toks(c)-{'north','south','east','west','northern','southern','phoenix','dallas','chicago','atlanta','denver','austin','houston','reno','vegas','las','san','los','new','york','kansas','st','saint','fort','lake','park','valley','city','county','one','two','three','building','bldg','phase'} for f,c in zip(m.facility_name,m.city)]
doc['_city']=doc.city.map(city_n); doc['_addr']=doc.address.map(addr_key); doc['_toks']=(doc.facility.fillna('')+' '+doc.owner.fillna('')).map(toks)
doc['_zip']=doc.zip.astype(str).str.extract(r'(\d{5})')[0]
m['_zip']=m.zip_code.astype(str).str.extract(r'(\d{5})')[0]
bys={st:g for st,g in doc.groupby('state')}
rows=[]
for i,r in m.iterrows():
    g=bys.get(r.state)
    if g is None: rows.append((None,None)); continue
    hit=None
    if r._addr is not None:
        h=g[g._addr==r._addr]
        if len(h): hit=('addr',h)
    if hit is None and r._city:
        h=g[(g._city==r._city)]
        if len(h):
            ok=h[h._toks.map(lambda t: len(t & r._optok)>=1 or (len(r._ftok)>=2 and len(t & r._ftok)>=2))]
            if len(ok): hit=('city+op',ok)
    if hit is None and pd.notna(r._zip):
        h=g[g._zip==r._zip]
        if len(h):
            ok=h[h._toks.map(lambda t: len(t & r._optok)>=1)]
            if len(ok): hit=('zip+op',ok)
    if hit is None: rows.append((None,None))
    else: rows.append((hit[0], hit[1]))
m['match_how']=[r[0] for r in rows]
m['match_docs']=[';'.join(r[1].index[:5]) if r[1] is not None else None for r in rows]
m['match_n_docs']=[len(r[1]) if r[1] is not None else 0 for r in rows]
m['match_gen_units']=[r[1].gen_units.max() if r[1] is not None else 0 for r in rows]
m['match_in_sites']=[r[1].in_sites.notna().any() if r[1] is not None else False for r in rows]
m['match_fac']=[r[1].facility.iloc[0] if r[1] is not None else None for r in rows]
m.to_parquet(f'{SP}/lbl_join2.parquet')
print('map facilities',len(m),'matched',m.match_how.notna().sum()); print(m.match_how.value_counts())
big=m[(m.demand_capacity_mw>=50)|(m.it_load_mw>=30)|(m.status.isin(['Under Construction','Planned']))]
print('large/planned',len(big),'matched',big.match_how.notna().sum(),'in sites',big.match_in_sites.sum())
st=m.groupby('state').agg(n=('facility_name','size'),matched=('match_how',lambda x:x.notna().sum()),in_sites=('match_in_sites','sum'),
   big=('demand_capacity_mw',lambda x:(x>=50).sum()),planned=('status',lambda x:x.isin(['Under Construction','Planned']).sum()))
st['pct_matched']=(100*st.matched/st.n).round(0)
print(st.sort_values('n',ascending=False).head(40).to_string())
pd.set_option('display.width',250); pd.set_option('display.max_colwidth',40)
print('--- matched to union but NOT in sites, with gen units, sorted by gen units:')
x=m[m.match_how.notna()&~m.match_in_sites&(m.match_gen_units>=1)].sort_values('match_gen_units',ascending=False)
print(x[['state','facility_name','operator','city','status','demand_capacity_mw','match_how','match_fac','match_gen_units','match_docs']].head(80).to_string())
