import re, os, sys, json, time
from collections import Counter
from multiprocessing import Pool
ROOT='/home/afhubbard/permit_data_extraction/data/interim'
DIRS=['extracted_text','dc_generator_text']
OUT=sys.argv[1]
PATS={
 'dc_core': r"data[ -]?cent(?:er|re)s?|datacenter|server farm|hyperscale|cloud (?:campus|computing)|comput(?:e|ing) (?:campus|facility)|server (?:hall|room|rack)|IT load|IT equipment|artificial intelligence|\bAI (?:campus|data|training|computing|workload|facility|infrastructure)|digital infrastructure|information technology (?:facility|campus|center)",
 'dc_weak': r"co-?location|uninterruptible power|\bUPS\b|\bGPUs?\b|crypto|bitcoin|blockchain|digital asset|mining (?:rig|machine|hardware)|high[- ]performance comput|\bHPC\b|static transfer switch|\bPDUs?\b|computer (?:equipment|room|floor)",
 'btm': r"behind[- ]the[- ]meter|island(?:ed)? mode|islanded|not (?:be )?(?:inter)?connected to the (?:grid|utility|electric)|off[- ]grid|on[- ]?site (?:power|generation|electricity|electrical generation)|private use network|\bPUN\b|microgrid|prime power|self[- ]gen|bridge power|bridging power|captive (?:power|generation)|not be capable of selling|will not (?:sell|export)|no (?:grid|utility) (?:connection|interconnection)|primary (?:source of )?power|continuous duty|non-emergency (?:engine|generator|turbine)|dedicated (?:power plant|generation)",
 'naics': r"518210|\b5182\b|NAICS[^\n]{0,25}518|541511|541512|541519|519130|519190|SIC[^\n]{0,25}7374|\b7374\b|\b7379\b",
 'ops': r"amazon|vadata|microsoft|google|alphabet inc|design llc|meta platforms|facebook|oracle|equinix|digital realty|cyrusone|\bqts\b|quality technology|vantage data|aligned (?:data|energy)|compass datacenters|edgeconnex|cloudhq|databank|flexential|novva|sabey|t5 data|prime data centers|cologix|coresite|iron mountain|ntt global|ntt data|stack infrastructure|switch,? (?:inc|ltd|llc)|skybox|stream data|crusoe|coreweave|lambda,? inc|\bxai\b|x\.ai|openai|tract capital|rowan digital|powerhouse data|corscale|yondr|ada infrastructure|datavault|core scientific|applied digital|cipher mining|riot platforms|riot blockchain|marathon digital|terawulf|hut 8|galaxy digital|\biren\b|iris energy|bitdeer|sailion|poolside|fermi america|nebius|lancium|stargate|colossus|hyperion|prometheus hyperscale|nscale|related digital|soluna|bitfarms|cleanspark|mawson|cormint|compute north|wyoming hyperscale|tallgrass|voltagrid|proenergy|fluidstack|anthropic|sandow lakes|hunt energy|kilby|nexus data|longhorn data|gw ranch|pacifico gw|vantage frontier|blackstone|tierpoint|centersquare|cyxtera|lumen technologies|cogent communications|mapletree|element critical|evoque|edged energy|nvidia|supermicro|apple inc|salesforce|linkedin|dropbox|rackspace|cloudflare|akamai|softlayer|zayo|windstream|centurylink|level 3 communications|internap|servercentral|365 data|datasite|expedient|involta|lightedge|netrality|peak 10|sungard|us signal|vxchnge|digital fortress|h5 data|infomart|colovore|scala data|edgecore|edge centres|edgepresence|amazon web|\baws\b|google llc|oracle america|switch ltd|cyrus one|quality technology services|ragingwire|nautilus data|dupont fabros|dft|sentinel data|carter validus|landmark|american tower|1547 critical|ubiquity|prime data|corescale|bitmain|core weave|terawulf|greenidge|stronghold digital|argo blockchain|us bitcoin|whinstone|rhodium|compute north|foundry digital|gryphon digital|cleanspark|marathon|bit digital|hive digital|northern data|digihost|sphere 3d|cathedra|lm funding|american bitcoin|abundant mines|texas blockchain|bitnile|ault alliance|hyperscale|aligned|vantage|cyrusone|qts|edgeconnex|compass"
}
RX={k:re.compile(v,re.I) for k,v in PATS.items()}
SNIP={'dc_core':6,'btm':6,'naics':3,'ops':0,'dc_weak':0}
def proc(path):
    try:
        with open(path,'r',errors='ignore') as f: t=f.read()
    except Exception as e:
        return None
    rec={'doc':os.path.splitext(os.path.basename(path))[0],'dir':os.path.basename(os.path.dirname(path)),'nchars':len(t),'g':{}}
    for k,rx in RX.items():
        c=Counter(); snips=[]; n=0
        for m in rx.finditer(t):
            n+=1
            c[re.sub(r'\s+',' ',m.group(0).lower())]+=1
            if len(snips)<SNIP[k]:
                a=max(0,m.start()-160); b=min(len(t),m.end()+160)
                snips.append(re.sub(r'\s+',' ',t[a:b]))
        if n: rec['g'][k]={'n':n,'top':dict(c.most_common(12)),'snips':snips}
    return rec
if __name__=='__main__':
    files=[]
    for d in DIRS:
        p=os.path.join(ROOT,d)
        files+= [os.path.join(p,f) for f in os.listdir(p) if f.endswith('.txt')]
    print('files',len(files),flush=True)
    t0=time.time(); n=0
    with Pool(11) as pool, open(OUT,'w') as out:
        for rec in pool.imap_unordered(proc, files, chunksize=50):
            n+=1
            if rec and rec['g']: out.write(json.dumps(rec)+'\n')
            if n%10000==0: print(n, round(time.time()-t0), flush=True)
    print('done',n,round(time.time()-t0),flush=True)
