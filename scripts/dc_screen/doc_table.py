import pandas as pd, numpy as np, re
SP='data/processed/analysis/dc_screen/work'
df=pd.read_parquet(f'{SP}/v5d_work.parquet')
print('rows',len(df),'docs',df.Filename.nunique())
print(df['Source Run'].value_counts(dropna=False).head(15))
print(df.Status.value_counts().head())
# --- unit-level generator signature
ut=(df['Unit Type'].fillna('')+' | '+df['Unit Description'].fillna('')+' | '+df['Unit Make'].fillna('')+' | '+df['Unit Model'].fillna('')).str.lower()
is_gen=ut.str.contains(r'generat|genset|engine|turbine|reciprocat|combustion turbine|\bct\b|\bic\b|\brice\b|\bcogen|\bcogeneration', regex=True)
is_turb=ut.str.contains(r'turbine|\bct-?\d|combustion turb|\bcc-?\d|combined[- ]cycle|simple[- ]cycle|\bsgt|\blm\d{4}|\b7ha|\b7f\b|\b501|aeroderiv', regex=True)
is_emerg=ut.str.contains(r'emergenc|standby|stand-by|backup|back-up', regex=True)
is_fire=ut.str.contains(r'fire ?pump|firewater', regex=True)
def num(s):
    return pd.to_numeric(s.astype(str).str.replace(',','').str.extract(r'([-+]?\d*\.?\d+)')[0], errors='coerce')
qty=num(df['Unit Quantity']).fillna(1).clip(1,2000)
gc=df['Generation Capacity'].fillna('').astype(str).str.lower()
gcv=num(gc); 
mw=np.where(gc.str.contains('mw'), gcv, np.where(gc.str.contains('kw'), gcv/1000, np.where(gc.str.contains('hp'), gcv*0.000746, np.nan)))
cv=num(df['Capacity Value']); cu=df['Capacity Unit'].fillna('').astype(str).str.lower()
mw2=np.where(cu.str.contains('mw')&~cu.str.contains('mmbtu'), cv, np.where(cu.str.contains('kw'), cv/1000, np.where(cu.str.contains('hp'), cv*0.000746, np.nan)))
mw=pd.Series(mw).fillna(pd.Series(mw2))
df['_gen']=is_gen; df['_turb']=is_gen&is_turb; df['_emerg']=is_gen&is_emerg; df['_fire']=is_fire; df['_qty']=qty; df['_mw']=mw
fuel=df['Fuel Type'].fillna('').str.lower()
df['_diesel']=is_gen&fuel.str.contains('diesel|ulsd|distillate|no\\. ?2|fuel oil')
df['_gas']=is_gen&fuel.str.contains('natural gas|\\bng\\b|pipeline|methane|lng|propane')
g=df[df._gen].groupby('Filename')
gen=pd.DataFrame({
 'gen_rows':g.size(),
 'gen_units':g._qty.sum(),
 'emerg_units':g.apply(lambda x: x.loc[x._emerg,'_qty'].sum()),
 'turb_units':g.apply(lambda x: x.loc[x._turb,'_qty'].sum()),
 'diesel_units':g.apply(lambda x: x.loc[x._diesel,'_qty'].sum()),
 'gas_gen_units':g.apply(lambda x: x.loc[x._gas,'_qty'].sum()),
 'max_unit_mw':g._mw.max(),
 'n_units_ge1mw':g.apply(lambda x: x.loc[x._mw>=1,'_qty'].sum()),
 'gen_mw_sum':g.apply(lambda x: (x._mw.fillna(0)*x._qty).sum()),
})
# --- doc-level fields
first=df.groupby('Filename').agg(**{
 'status':('Status','first'),'state':('Facility State Abbreviation','first'),'facility':('Facility Name','first'),'owner':('Owner/Operator Name','first'),
 'address':('Facility Address','first'),'city':('Facility City','first'),'county':('Facility County','first'),'zip':('Facility Zip Code','first'),
 'naics':('NAICS Code','first'),'naics_cls':('Classified NAICS','first'),'naics_res':('NAICS Resolved','first'),'sic':('SIC Code','first'),
 'industry':('Industry Description','first'),'permit':('Permit Number','first'),'permit_type':('Permit Type','first'),'issued':('Issuance Date','first'),
 'authority':('Regulatory Authority','first'),'source_run':('Source Run','first'),'n_rows':('Unit ID','size')})
doc=first.join(gen).fillna({'gen_rows':0,'gen_units':0,'emerg_units':0,'turb_units':0,'diesel_units':0,'gas_gen_units':0,'n_units_ge1mw':0,'gen_mw_sum':0})
# NAICS DC flag
def nflag(row):
    vals=[str(row[c]) for c in ['naics','naics_cls','naics_res'] if pd.notna(row[c])]
    s=' '.join(vals)
    return bool(re.search(r'\b5182\d*|\b51821|\b54151\d|\b5191\d|\b519130|\b519190|\b518\b', s)) or bool(re.search(r'\b7374\b|\b7379\b', str(row['sic'])))
doc['naics_dc']=doc.apply(nflag, axis=1)
OPS=r"amazon|vadata|microsoft|google|alphabet|design llc|meta platforms|facebook|oracle|equinix|digital realty|cyrusone|cyrus one|\bqts\b|quality technology|vantage|aligned|compass data|edgeconnex|cloudhq|databank|flexential|novva|sabey|t5 |prime data|cologix|coresite|iron mountain|ntt |stack infra|switch|skybox|stream data|crusoe|coreweave|lambda|\bxai\b|x\.ai|openai|tract|rowan digital|powerhouse|corscale|yondr|ada infra|datavault|core scientific|applied digital|cipher|riot |marathon digital|terawulf|hut 8|galaxy digital|\biren\b|iris energy|bitdeer|sailion|poolside|fermi|nebius|lancium|stargate|colossus|hyperion|prometheus|nscale|related digital|soluna|bitfarms|cleanspark|mawson|cormint|compute north|hyperscale|tallgrass|voltagrid|proenergy|fluidstack|anthropic|sandow|hunt energy|kilby|nexus|longhorn|gw ranch|pacifico|blackstone|tierpoint|centersquare|cyxtera|lumen|cogent|mapletree|element critical|evoque|edged|nvidia|supermicro|apple|salesforce|linkedin|dropbox|rackspace|cloudflare|akamai|zayo|windstream|centurylink|level 3|internap|servercentral|365 data|datasite|expedient|involta|lightedge|netrality|peak 10|sungard|us signal|vxchnge|digital fortress|h5 data|infomart|colovore|scala|edgecore|\baws\b|ragingwire|nautilus|dupont fabros|sentinel data|1547|ubiquity|bitmain|greenidge|stronghold|argo|whinstone|rhodium|foundry|gryphon|bit digital|hive|northern data|digihost|cathedra|american bitcoin|bitnile|data ?cent|datacenter|server|cloud|comput|digital|\bdc\b|technology|technologies|hosting|colocation|\bit\b"
nm=(doc.facility.fillna('')+' | '+doc.owner.fillna('')+' | '+doc.industry.fillna('')).str.lower()
doc['name_ops_hit']=nm.str.extract('('+OPS+')')[0]
doc['name_dc_word']=nm.str.contains(r'data ?cent|datacenter|server farm|colocation|hyperscale|cloud', regex=True)
# sites source docs
s=pd.read_csv('/home/afhubbard/permit_data_extraction/data/processed/analysis/dc_generation_sites.csv')
sd={}
for _,r in s.iterrows():
    for d in str(r.source_files).split(';'):
        sd[d.strip()]=r.evidence_tier
doc['in_sites']=doc.index.map(lambda x: sd.get(x))
doc.to_parquet(f'{SP}/doc_table.parquet')
print(doc.shape)
print('in_sites', doc.in_sites.value_counts(dropna=False))
print('naics_dc docs', doc.naics_dc.sum(), 'of which in sites', doc[doc.naics_dc].in_sites.notna().sum())
print('name_dc_word docs', doc.name_dc_word.sum(), 'in sites', doc[doc.name_dc_word].in_sites.notna().sum())
big=doc[(doc.emerg_units>=8)&(doc.max_unit_mw>=1)]
print('big emergency fleets (>=8 units, >=1MW)', len(big), 'in sites', big.in_sites.notna().sum())
print(big[big.in_sites.isna()].sort_values('emerg_units',ascending=False)[['state','facility','owner','city','naics','emerg_units','max_unit_mw','issued','source_run']].head(60).to_string())
