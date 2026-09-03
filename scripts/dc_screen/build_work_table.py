import pyarrow as pa, pyarrow.csv as pc, pyarrow.parquet as pq, sys, time
t=time.time()
cols=['Filename','Status','Facility Name','Owner/Operator Name','Facility Address','Facility City','Facility State Abbreviation','Facility Zip Code','Facility County','NAICS Code','Classified NAICS','NAICS Resolved','SIC Code','Industry Description','Unit ID','Unit Description','Unit Quantity','Unit Make','Unit Model','Year of Manufacture','Unit Type','Capacity Value','Capacity Unit','Fuel Type','Generation Capacity','Annual Run Hours','Permit Number','Permit Type','Issuance Date','Regulatory Authority','Source Run']
src='data/processed/permit_data_union_v5d.csv'
ro=pc.ReadOptions(block_size=64<<20)
co=pc.ConvertOptions(include_columns=cols, column_types={c:pa.string() for c in cols}, strings_can_be_null=True)
reader=pc.open_csv(src, read_options=ro, convert_options=co)
out='data/processed/analysis/dc_screen/work/v5d_work.parquet'
w=None; n=0
for batch in reader:
    tbl=pa.Table.from_batches([batch])
    if w is None: w=pq.ParquetWriter(out, tbl.schema, compression='zstd')
    w.write_table(tbl); n+=tbl.num_rows
w.close()
print('rows',n,'secs',round(time.time()-t))
