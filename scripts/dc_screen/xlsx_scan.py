import openpyxl, glob, pandas as pd, json, time
SP='data/processed/analysis/dc_screen/work'
c=pd.read_parquet(f'{SP}/corpus_index.parquet'); missing=set(c[(~c.in_union)&(c.log=='success')].index)
found={}; rows_per_file={}
for p in sorted(glob.glob('data/processed/permit_data_2026-*.xlsx')):
    t=time.time()
    try:
        wb=openpyxl.load_workbook(p,read_only=True); ws=wb.worksheets[0]
        it=ws.iter_rows(values_only=True); hdr=next(it)
        fi=[i for i,h in enumerate(hdr) if h and str(h).strip().lower()=='filename']
        if not fi: print(p,'no Filename col',hdr[:5]); continue
        fi=fi[0]; n=0; hit=set()
        for r in it:
            n+=1; fn=r[fi]
            if fn is None: continue
            fn=str(fn).rsplit('.txt',1)[0].rsplit('.pdf',1)[0]
            if fn in missing: hit.add(fn)
        rows_per_file[p]=(n,len(hit))
        for h in hit: found.setdefault(h,[]).append(p.split('/')[-1])
        print(p.split('/')[-1],'rows',n,'missing-docs-found',len(hit),round(time.time()-t),'s',flush=True)
    except Exception as e: print(p,'ERR',e)
print('missing docs',len(missing),'found in some xlsx',len(found))
json.dump({k:v for k,v in found.items()},open(f'{SP}/xlsx_found.json','w'))
