#!/usr/bin/env python3
"""Build the data-center self-generation site table from a union CSV.

Replaces the lost scratchpad builder (dc_gen_v4.py, session 2430b4a5) with a
checked-in, re-runnable pipeline:

  1. slim the union to the columns needed (pyarrow, streaming; cached parquet)
  2. classify every document: is it a data-center permit?  Signals are the
     facility/owner name, NAICS/SIC, the dedicated DC scrape lanes, and the
     full-text screen produced by scripts/dc_screen (term counts per document
     in <work-dir>/text_hits2.parquet and context snippets in
     <work-dir>/snips.txt).  Institutional fleets (universities, bases,
     hospitals, refineries ...) are rejected unless the name says data center.
  3. cluster documents into facilities with union-find over lane facility ids
     (TX regulated-entity number, AZ/NV/OR/GA/OH/VA ids), street number +
     street name, permit numbers, and city + distinctive name tokens
  4. roll up generator units with the rules learned in August: per-unit MW
     from kW/MW electrical ratings (hp only as a fallback), plausibility
     ceilings that turn "fleet total next to 'each'" values back into per-unit
     values, de-duplication of restated units (same unit id, hp restatements
     of kW rows, simple-cycle / combined-cycle twins), and a count of units
     that carry no rating at all
  5. carry lat/lon and capacity-gap estimates forward from the prior table
     where the site is the same; new sites get coordinates parsed from the
     permit text, else a city centroid, else a county centroid
  6. estimate missing MW where the permit names a turbine/engine model
     (class knowledge, kept in separate est_missing_mw_* columns)

Usage (from the repo root):
    PYTHONPATH=. python scripts/build_dc_generation_sites.py \
        --union data/processed/permit_data_union_v5e.csv \
        --work-dir data/processed/analysis/dc_screen/work \
        --prior-sites data/processed/analysis/dc_generation_sites.csv \
        --out data/processed/analysis/dc_generation_sites.csv

Run scripts/dc_screen/grep_screen.sh + agg2.py + snips.sh first so the text
signals exist; the builder works without them but then sees only name/NAICS/
lane evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pc
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
TEXT_DIRS = [REPO / "data/interim/extracted_text", REPO / "data/interim/dc_generator_text"]
TCEQ_INDEX = REPO / "data/raw/tx_tceq_dc_nsr_permits/tx_tceq_dc_nsr_index.csv"
CITY_CENTROIDS = REPO / "data/processed/dashboard/city_centroids.parquet"
COUNTY_SHP = REPO / "data/external/cb_2018_us_county_500k/cb_2018_us_county_500k.shp"

COLS = ['Filename', 'Status', 'Facility Name', 'Owner/Operator Name', 'Facility Address', 'Facility City',
        'Facility State Abbreviation', 'Facility Zip Code', 'Facility County', 'NAICS Code', 'Classified NAICS',
        'NAICS Resolved', 'SIC Code', 'Industry Description', 'Unit ID', 'Unit Description', 'Unit Quantity',
        'Unit Make', 'Unit Model', 'Year of Manufacture', 'Unit Type', 'Capacity Value', 'Capacity Unit',
        'Fuel Type', 'Generation Capacity', 'Annual Run Hours', 'Permit Number', 'Permit Type', 'Issuance Date',
        'Regulatory Authority', 'Source Run']

FIPS = {'01': 'AL', '02': 'AK', '04': 'AZ', '05': 'AR', '06': 'CA', '08': 'CO', '09': 'CT', '10': 'DE', '11': 'DC',
        '12': 'FL', '13': 'GA', '15': 'HI', '16': 'ID', '17': 'IL', '18': 'IN', '19': 'IA', '20': 'KS', '21': 'KY',
        '22': 'LA', '23': 'ME', '24': 'MD', '25': 'MA', '26': 'MI', '27': 'MN', '28': 'MS', '29': 'MO', '30': 'MT',
        '31': 'NE', '32': 'NV', '33': 'NH', '34': 'NJ', '35': 'NM', '36': 'NY', '37': 'NC', '38': 'ND', '39': 'OH',
        '40': 'OK', '41': 'OR', '42': 'PA', '44': 'RI', '45': 'SC', '46': 'SD', '47': 'TN', '48': 'TX', '49': 'UT',
        '50': 'VT', '51': 'VA', '53': 'WA', '54': 'WV', '55': 'WI', '56': 'WY', '72': 'PR'}

# ----------------------------------------------------------------------------
# name / text classification
# ----------------------------------------------------------------------------
STOP = set('llc inc corp corporation company co lp ltd the of and data center centers datacenter campus facility '
           'site services service llp plc group holdings properties property partners ventures dba a an at in for '
           'environmental incorporated none nan error null unknown'.split())
CITY_TOKENS = set('north south east west northern southern central new city county fort saint st lake park valley '
                  'san los las one two three building bldg phase'.split())
GENERIC_NAME_TOKENS = set(
    'technology technologies realty trust investment investments capital development partners associates enterprises '
    'industries international national american global systems solutions network networks communications '
    'telecommunications wireless infrastructure equipment power energy electric generation plant station project '
    'operations management owner holdco propco opco fund reit campus drive road street parkway circle court place '
    'plaza blvd boulevard highway lane way avenue suite corporate business office complex tech park center centre '
    'amazon microsoft google alphabet vadata meta facebook oracle equinix cyrusone qts vantage aligned compass stream '
    'digital realty databank edgeconnex cloudhq flexential sabey cologix coresite iron mountain ntt stack switch tract '
    'powerhouse corscale yondr novva prime skybox quality properties datacenters datacenter services service '
    'virginia texas arizona georgia oregon nevada ohio indiana illinois carolina california washington maryland '
    'pennsylvania jersey york florida michigan minnesota missouri utah wisconsin iowa nebraska kansas oklahoma '
    'louisiana mississippi tennessee kentucky alabama colorado mexico wyoming montana dakota'.split())
CID_PREFIXES = ('iad', 'dfw', 'pdx', 'rno', 'sat', 'cmh', 'phx', 'atl', 'aus', 'hou', 'acc', 'nva', 'dal', 'den', 'ind',
                'ord', 'chi', 'sea', 'lax', 'sjc', 'mci', 'slc', 'las', 'bwi', 'dca', 'ewr', 'nyc', 'mia', 'cle', 'cin',
                'col', 'dc', 'va', 'tx', 'az', 'ga', 'oh', 'or', 'nv', 'il', 'pa', 'nj', 'ny', 'ca', 'wa', 'md', 'nc',
                'sc', 'mn', 'mo', 'ut', 'wi', 'ia', 'ne', 'ks', 'ok', 'la', 'ms', 'tn', 'ky', 'al', 'fl', 'mi', 'in', 'co',
                'nm', 'wy', 'id', 'mt', 'nd', 'sd', 'ct', 'ma', 'wv', 'ar', 'lc')

# verified false positives (BACT boilerplate 'such as data centers'; one on-site genset at a peaker)
EXCLUDE_NAMES = re.compile(r"sycamore riverside|j\.?\s?k\.? smith", re.I)
DC_NAME = re.compile(r"data ?cent(?:er|re)s?|datacenter|server farm|colocation|co-location|hyperscale|cloud campus", re.I)
OPERATOR = re.compile(
    r"amazon data|vadata|amazon\.com services|microsoft|google|alphabet|meta platforms|facebook|oracle|equinix|"
    r"digital realty|cyrusone|cyrus one|\bqts\b|quality technology|quality investment|vantage data|vantage data centers|"
    r"aligned (?:data|energy)|compass data|edgeconnex|cloudhq|databank|flexential|novva|sabey|t5 data|prime data|"
    r"cologix|coresite|iron mountain data|ntt global|ntt data|stack infra|switch,? (?:inc|ltd)|skybox|stream data|"
    r"crusoe|coreweave|lambda,? inc|\bxai\b|x\.ai|openai|tract capital|rowan digital|powerhouse data|corscale|yondr|"
    r"ada infra|datavault|core scientific|applied digital|cipher (?:mining|[a-z]+ llc)|riot platforms|marathon digital|"
    r"terawulf|hut 8|galaxy (?:digital|helios)|\biren\b|iris energy|bitdeer|sailion|poolside|fermi (?:america|equipment)|"
    r"nebius|lancium|nscale|soluna|cleanspark|mawson|fluidstack|anthropic|kilby|nexus (?:data|hubbard)|longhorn data|"
    r"gw ranch|pacifico gw|tierpoint|centersquare|cyxtera|element critical|evoque|kyndryl|serverfarm|rackspace|"
    r"data foundry|\bdtcc\b|godaddy|verizon business|\baol\b|neustar|terremark|unisys|gannett|freddie mac|"
    r"science applications international|meridian science|elk ventures|gazelle ventures|scout development|"
    r"darab ventures|beaumeade|hatchworks|lavender fields|life cycle power|clop jeffersonville|blocke llc|netrality|"
    r"cerner|turnstile|montauk innovations|bistrozzi|maguro enterprises|foxman llc|sidecat|gable corp|siculus|tapaha|"
    r"cmh\d{3}|\biad-? ?\d|\bpdx-? ?\d|\bdfw-? ?\d|\brno ?\d|\bsat ?\d{3}|project (?:granite|marble|jupiter)|"
    r"table mountain industries|lifeline data|xenergy|blueprint mining|mfi hpe|hedgehog power|acoma,? llc|east microgrid|"
    r"west microgrid|franklin farms|blue power|nydig|bitmain|whinstone|cormint|compute north|mzx tech|questa llc|gable corp", re.I)
INSTITUTIONAL = re.compile(
    r"universit|college|hospital|health|medical|air force|\barmy\b|navy|naval|marine corps|\bbase\b|school|prison|"
    r"correction|airport|arsenal|disney|resort|refiner|semiconductor|micron|steel|paper|chemical|pharma|laborator|"
    r"casino|stadium|hotel|city of|\bmill\b|cement|foundry|automotive|assembly plant|manufactur|distill|grain|barge|"
    r"asphalt|construction|contractor|excavat|highway|quarry|gravel|landfill|waste|resource recovery|packaging|terminal|"
    r"pipeline|compressor|gas plant|extraction plant|well pad|petroleum|coating|foods|fertilizer|glass|tire|rubber|"
    r"lumber|hardwood|library|dealer|truck|metal recycling|insurance|missile|space force|federal center|raytheon|"
    r"boeing|lockheed|merck|glaxo|eli lilly|cummins|rolls-royce|allison|carvana|entek|haynes|cf industries|freeport|"
    r"georgia-pacific|louis dreyfus|johns manville|tokai|caesars", re.I)
TIED = re.compile(r"(proposed|new|the|this|a|an|its|their|planned|existing|adjacent|on-site|onsite|hyperscale|"
                  r"colocation|co-location|enterprise|commercial|large|mission[- ]critical|ai|cloud)\s+"
                  r"(data ?cent(er|re)s?|datacenters?)|data ?cent(er|re)s?\s+(campus|facility|facilities|project|"
                  r"building|buildings|operations?|will|would|is|are|shall|located|consist|that|which|at|in|owned|"
                  r"operated|to be|and its|site|complex|use|load|tenant|customer|construction|development|phase|"
                  r"expansion|market)|(serve|serving|support|supporting|power|powering|for|at)\s+(the|a|an|its|this)?"
                  r"\s*(data ?cent(er|re)|datacenter)|behind[- ]the[- ]meter|islanded|private use network|"
                  r"not be capable of selling|will not (sell|export)|server (hall|room|rack)|hyperscale|colocation|"
                  r"co-location|crypto|bitcoin|blockchain|digital asset", re.I)
BOILER = re.compile(r"national climatic data center|climatic data|central data (exchange|center)|epa.{0,20}data center|"
                    r"air quality data center|such as data centers|(e\.g\.|including|like|for example),?\s*(hospitals|"
                    r"data centers)|data centers,? (hospitals|telecommunications|and other)|(hospitals|schools|nursing "
                    r"homes|telecommunications facilities),? (and )?data centers|nsps|neshap|subpart|source category|"
                    r"typical (of|for) data centers|areas such as data centers|applications (such as|including) data "
                    r"centers|industries (such as|including) data centers|emergency generators (at|for) (hospitals|data "
                    r"centers)|www\.|\.gov", re.I)

GEN_RX = re.compile(r"generat|genset|gen-set|engine|turbine|reciprocat|\bcogen|\bcombustion turb|\brice\b", re.I)
TURB_RX = re.compile(r"turbine|\bctg?-?\d|combustion turb|combined[- ]cycle|simple[- ]cycle|\bsgt[- ]?\d|\blm\d{4}|\b7ha\b|"
                     r"\bge (?:7f|7e|6b|7ha)|frame (?:6|7|9)|m501|aeroderiv|\bgtg?-?\d|titan \d|taurus \d|mars \d|centaur \d|"
                     r"tm2500|lms100|trent 60|\bft8\b|swiftpac", re.I)
EMERG_RX = re.compile(r"(?<!non-)(?<!non )emergenc|standby|stand-by|backup|back-up|black ?start", re.I)
FIRE_RX = re.compile(r"fire ?pump|firewater|fire water", re.I)
PRIME_RX = re.compile(r"non-?\s?emergency|prime power|prime[- ]rated|continuous|behind[- ]the[- ]meter|base ?load|microgrid|power plant|generation plant|generating plant|peaking|demand response|load (?:shed|shave)|grid support|export", re.I)
NONGEN_RX = re.compile(r"boiler|heater|flare|tank|cooling tower|chiller|belly tank|fuel tank|storage|scrubber|"
                       r"oxidizer|silo|dryer|furnace|kiln|paint|conveyor|crusher|pump(?!.*generat)", re.I)

# turbine / engine nameplate class knowledge (MW per unit) — used ONLY for est_missing_mw columns
MODEL_RATINGS = [
    (r"7ha\.?03|7ha 03", 430, 'turbine'), (r"7ha\.?02|7ha 02", 384, 'turbine'), (r"7ha\.?01", 290, 'turbine'),
    (r"9ha", 571, 'turbine'), (r"m501jac|501jac", 440, 'turbine'), (r"m501j\b", 330, 'turbine'),
    (r"sgt6-9000hl|9000hl", 440, 'turbine'), (r"dgt6-300h", 380, 'turbine'), (r"sgt6-5000f|5000f", 250, 'turbine'),
    (r"7f\.?05|7fa\.?05", 240, 'turbine'), (r"7fa|7f\b", 180, 'turbine'), (r"7ea|7e\b", 85, 'turbine'),
    (r"6b\b|6ba", 45, 'turbine'), (r"6fa|6f\.03", 80, 'turbine'), (r"sgt-800|sgt800", 57, 'turbine'),
    (r"sgt-750", 40, 'turbine'), (r"sgt-400", 13, 'turbine'), (r"sgt-300", 8, 'turbine'), (r"sgt-a65|trent 60", 60, 'turbine'),
    (r"sgt-a45|tm2500", 34, 'turbine'), (r"lm6000", 50, 'turbine'), (r"lm2500\+?g4|lm2500", 30, 'turbine'),
    (r"lm9000", 75, 'turbine'), (r"lms100", 100, 'turbine'), (r"ft8|swiftpac", 30, 'turbine'),
    (r"titan 130", 15, 'turbine'), (r"titan 250", 22, 'turbine'), (r"taurus 60", 5.7, 'turbine'), (r"taurus 70", 8, 'turbine'),
    (r"mars 100", 11, 'turbine'), (r"centaur 50", 4.6, 'turbine'), (r"saturn 20", 1.2, 'turbine'),
    (r"c175-20|c175 20", 4.0, 'engine'), (r"c175-16|c175 16", 3.0, 'engine'), (r"3516e", 3.1, 'engine'),
    (r"3516c-hd|3516c hd", 2.5, 'engine'), (r"3516c|3516b|3516", 2.0, 'engine'), (r"3512c|3512", 1.5, 'engine'),
    (r"g3516h", 2.5, 'engine'), (r"g3520", 2.0, 'engine'), (r"cg260", 4.0, 'engine'), (r"cg170", 2.0, 'engine'),
    (r"18v50sg|50sg", 18.8, 'engine'), (r"20v34sg|34sg", 9.7, 'engine'), (r"j920", 10.4, 'engine'), (r"j624", 4.4, 'engine'),
    (r"j620", 3.3, 'engine'), (r"qsk95", 3.5, 'engine'), (r"qsk78", 3.0, 'engine'), (r"qsk60", 2.0, 'engine'),
    (r"qsk50", 1.5, 'engine'), (r"mtu 20v4000|20v4000", 3.0, 'engine'), (r"16v4000", 2.5, 'engine'), (r"12v4000", 1.5, 'engine'),
    (r"kta50", 1.5, 'engine'),
]


def tokens(x):
    x = re.sub(r"[^a-z0-9 ]", " ", str(x).lower())
    return {t for t in x.split() if t not in STOP and t not in CITY_TOKENS and len(t) >= 3 and not t.isdigit()}


def city_norm(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return ''
    x = re.sub(r"[^a-z ]", "", str(x).lower()).strip()
    x = re.sub(r"\bsaint\b", "st", x)
    return re.sub(r"\s+", " ", x)


def addr_key(x):
    m = re.match(r"\s*(\d+)\s+([a-z0-9]+)", str(x).lower())
    return f"{m.group(1)} {m.group(2)}" if m else None


def num(s):
    if s is None:
        return np.nan
    m = re.search(r"[-+]?\d[\d,]*\.?\d*", str(s))
    if not m:
        return np.nan
    try:
        return float(m.group(0).replace(',', ''))
    except ValueError:
        return np.nan


def permits_of(x):
    return {p.strip().lower() for p in re.split(r"[;,/]| and ", str(x)) if len(p.strip()) >= 4 and p.strip().lower() not in ('none', 'nan', 'error')}


# ----------------------------------------------------------------------------
# union loading
# ----------------------------------------------------------------------------
def load_union(union: Path, work: Path) -> pd.DataFrame:
    cache = work / (union.stem + "_work.parquet")
    if cache.exists() and cache.stat().st_mtime >= union.stat().st_mtime:
        return pd.read_parquet(cache)
    print(f"slimming {union} -> {cache}")
    co = pc.ConvertOptions(include_columns=COLS, column_types={c: pa.string() for c in COLS}, strings_can_be_null=True)
    reader = pc.open_csv(union, read_options=pc.ReadOptions(block_size=64 << 20), convert_options=co)
    w = None
    for batch in reader:
        t = pa.Table.from_batches([batch])
        if w is None:
            w = pq.ParquetWriter(cache, t.schema, compression='zstd')
        w.write_table(t)
    w.close()
    return pd.read_parquet(cache)


# ----------------------------------------------------------------------------
# unit-level parsing
# ----------------------------------------------------------------------------
NUM_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
             'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15, 'sixteen': 16, 'seventeen': 17,
             'eighteen': 18, 'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60,
             'seventy': 70, 'eighty': 80, 'ninety': 90, 'hundred': 100}


def parse_qty(qty, unit_id, desc):
    q = num(qty)
    if not np.isnan(q) and 1 <= q <= 2000:
        return int(q)
    for s in (str(unit_id), str(desc)):
        m = re.search(r"(\d{1,4})\s*(?:through|thru|to|-|–)\s*(\d{1,4})\b", s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < b - a < 2000 and b > a:
                return b - a + 1
    m = re.match(r"\s*(?:[A-Za-z-]+\s+)?\((\d{1,4})\)", str(desc))
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{1,4})\s*(?:x|×)\s", str(desc))
    if m:
        return int(m.group(1))
    return 1


RATING_RX = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:brake\s*)?(mm\s?btu|megawatts?|kilowatts?|horsepower|e?mwe?|e?kwe?|bhp|hp)\b", re.I)


def parse_rating(gen_cap, cap_val, cap_unit, desc):
    """Return (electric_mw, hp_mw, heat_mmbtu, rating_source)."""
    elec = hp = heat = np.nan
    src = None
    for text, tag in ((gen_cap, 'gen'), (f"{cap_val} {cap_unit}", 'cap'), (desc, 'desc')):
        for m in RATING_RX.finditer(str(text)):
            v = float(m.group(1).replace(',', '')) if m.group(1).replace(',', '').replace('.', '').isdigit() else np.nan
            u = m.group(2).lower()
            if np.isnan(v):
                continue
            u = {'megawatt': 'mw', 'megawatts': 'mw', 'kilowatt': 'kw', 'kilowatts': 'kw', 'horsepower': 'hp'}.get(u, u)
            if u.lstrip('e').startswith('mw') and np.isnan(elec):
                elec, src = v, tag
            elif u.lstrip('e').startswith('kw') and np.isnan(elec):
                elec, src = v / 1000.0, tag
            elif u in ('hp', 'bhp') and np.isnan(hp):
                hp = v * 0.000746
            elif u.replace(' ', '') == 'mmbtu' and np.isnan(heat):
                heat = v
        if not np.isnan(elec):
            break
    if np.isnan(elec):
        m = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*", str(gen_cap))
        if m:
            v = float(m.group(1).replace(',', ''))
            elec, src = (v / 1000.0, 'gen_bare_kw') if v >= 100 else (v, 'gen_bare_mw')
    return elec, hp, heat, src


CEILING = {'backup': 5.0, 'engine': 21.0, 'turbine': 1200.0}
RANGE_RX = re.compile(r"([A-Za-z][A-Za-z\-]{0,12}?)\s*(?:no\.?s?\.?|nos\.?|#)?\s*-?\s*(\d{1,4})\s*(?:through|thru|to|-|–|—)\s*(?:[A-Za-z][A-Za-z\-]{0,12}?\s*-?\s*)?(\d{1,4})\b", re.I)


def id_set(unit_id, desc):
    """Set of normalized unit ids covered by a row: ranges ('CEG1 through CEG234',
    'EG-1 through EG-120', 'Ref. Nos. 1 through 72'), lists ('GEN E1, GEN E2') or a
    single id.  Leading zeros are dropped so GEN001 == GEN1."""
    def norm(prefix, n):
        return re.sub(r"[^a-z]", "", prefix.lower()) + str(int(n))
    for s_ in (str(unit_id), str(desc)[:120]):
        m = RANGE_RX.search(s_)
        if m:
            a, b = int(m.group(2)), int(m.group(3))
            if b > a and b - a < 2000:
                return {norm(m.group(1), n) for n in range(a, b + 1)}
    ids = set()
    for part in re.split(r"[,;&]|\band\b", str(unit_id)):
        m = re.match(r"^\s*([A-Za-z][A-Za-z\-\. ]{0,12}?)\s*-?\s*(\d{1,4})\s*[A-Za-z]?\s*$", part)
        if m:
            ids.add(norm(m.group(1), m.group(2)))
    return ids or None


def unit_rows(df: pd.DataFrame) -> pd.DataFrame:
    ut = (df['Unit Type'].fillna('') + ' | ' + df['Unit Description'].fillna('') + ' | ' + df['Unit Make'].fillna('')
          + ' | ' + df['Unit Model'].fillna('') + ' | ' + df['Unit ID'].fillna(''))
    is_gen = ut.str.contains(GEN_RX) & ~ut.str.contains(FIRE_RX)
    u = df[is_gen].copy()
    ut = ut[is_gen]
    # ancillary equipment named after the units it serves (belly tanks "one in each diesel generator")
    anc = ut.str.contains(NONGEN_RX) & ~ut.str.contains(r"generator set|genset|gen-set|engine generator|turbine generator", case=False)
    u = u[~anc]
    ut = ut[~anc]
    u['is_turbine'] = ut.str.contains(TURB_RX)
    u['is_backup'] = ut.str.contains(EMERG_RX)
    u['is_prime_text'] = ut.str.contains(PRIME_RX)
    u['kind'] = np.where(u.is_turbine, 'turbine', np.where(u.is_backup, 'backup', 'engine'))
    u['cls'] = np.where(u.is_backup, 'backup/standby', 'prime/grid')
    u['qty'] = [parse_qty(q, i, d) for q, i, d in zip(u['Unit Quantity'], u['Unit ID'], u['Unit Description'])]
    r = [parse_rating(g, cv, cu, d) for g, cv, cu, d in zip(u['Generation Capacity'], u['Capacity Value'], u['Capacity Unit'], u['Unit Description'])]
    u['elec_mw'] = [x[0] for x in r]
    u['hp_mw'] = [x[1] for x in r]
    u['heat_mmbtu'] = [x[2] for x in r]
    u['rating_src'] = [x[3] for x in r]
    u['unit_mw'] = u.elec_mw.where(u.elec_mw.notna(), u.hp_mw)
    u['rating_from_hp'] = u.elec_mw.isna() & u.hp_mw.notna()
    # plausibility ceilings: a "per-unit" value above the ceiling next to qty>1 is a fleet total
    ceil = u.kind.map(CEILING)
    fleet = u.unit_mw.notna() & (u.unit_mw > ceil) & (u.qty > 1) & ((u.unit_mw / u.qty) <= ceil)
    u['capacity_flag'] = np.where(fleet, 'fleet_total_split', np.where(u.unit_mw.notna() & (u.unit_mw > ceil), 'implausible_dropped', ''))
    u.loc[fleet, 'unit_mw'] = u.loc[fleet, 'unit_mw'] / u.loc[fleet, 'qty']
    u.loc[u.capacity_flag == 'implausible_dropped', 'unit_mw'] = np.nan
    # genset-sized engines at data centers are emergency units unless the permit says otherwise
    genset = (u.kind == 'engine') & ~u.is_prime_text & (u.unit_mw.isna() | (u.unit_mw <= 5.0))
    u.loc[genset, 'kind'] = 'backup'
    u.loc[genset, 'cls'] = 'backup/standby'
    # a single row claiming >1,000 gensets is a certification / engine-family table, not a fleet
    big = (u.kind != 'turbine') & (u.qty > 1000)
    u.loc[big, 'capacity_flag'] = 'implausible_quantity'
    u.loc[big, 'unit_mw'] = np.nan
    u['group_mw'] = u.unit_mw * u.qty
    return u


def dedupe_units(u: pd.DataFrame) -> pd.DataFrame:
    """Within one facility: collapse restated unit groups.

    1. same normalized unit id (across documents) -> keep the most complete row
    2. simple-cycle / combined-cycle twins (same id root once SC/CC tokens are
       stripped, same quantity) -> keep the higher (combined-cycle) rating
    3. hp restatement of a kW row (same qty and kind, converted MW within 15%)
    4. the same (kind, class, qty, MW) group restated in another document
    Dropped rows are kept in the output with counted=False and a dedupe_flag.
    """
    u = u.copy()
    uid = u['Unit ID'].fillna('').str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
    generic = uid.isin(['', 'none', 'na', 'nan', 'error', 'unknown', 'various', 'multiple', 'all'])
    uid_root = uid.str.replace(r"simplecycle|combinedcycle|(?<=[a-z0-9])(?:sc|cc)(?=\d|$)", "", regex=True)
    desc = u['Unit Description'].fillna('').str.lower().str.replace(r"simple[- ]cycle|combined[- ]cycle|\(sc\)|\(cc\)|\s+", " ", regex=True).str.strip().str[:60]
    u['_uid'] = np.where(generic, None, uid)
    u['_root'] = np.where(generic, None, uid_root)
    u['_desc'] = desc + '|' + u.qty.astype(str) + '|' + u.unit_mw.round(2).astype(str) + '|' + u.kind
    u['_completeness'] = u[['Unit ID', 'Unit Make', 'Unit Model', 'Fuel Type', 'Issuance Date']].notna().sum(axis=1) + u.unit_mw.notna().astype(int) * 2 + u.unit_mw.fillna(0) / 1e4 + u.qty / 1e5
    u['_hp'] = u.rating_from_hp.astype(int)
    u = u.sort_values(['_hp', '_completeness'], ascending=[True, False])
    u['dedupe_flag'] = ''
    u['_ids'] = [id_set(i, d) for i, d in zip(u['Unit ID'], u['Unit Description'])]
    seen_id, seen_desc, seen_root, seen_kw, seen_group, seen_ids, seen_doc_fleet = set(), set(), {}, [], {}, set(), set()
    for idx in u.index:
        r = u.loc[idx]
        flag = ''
        if r._uid is not None and r._uid in seen_id:
            flag = 'same_unit_id'
        elif r._uid is None and r._desc in seen_desc:
            flag = 'same_description'
        elif r.qty >= 10 and not np.isnan(r.unit_mw) and (r.Filename, r.kind, r.qty, round(r.unit_mw, 2)) in seen_doc_fleet:
            flag = 'same_fleet_in_document'
        elif r._root is not None and (r._root, r.qty) in seen_root:
            flag = 'sc_cc_twin'
        elif r.rating_from_hp and not np.isnan(r.unit_mw) and any(q == r.qty and k == r.kind and abs(mw - r.unit_mw) / mw < 0.15 for q, mw, k in seen_kw):
            flag = 'hp_restatement'
        elif r._ids is not None and (r._ids & seen_ids):
            flag = 'overlapping_unit_ids'
        elif not np.isnan(r.unit_mw):
            gk = (r.kind, r.cls, r.qty, round(r.unit_mw, 1))
            if gk in seen_group and seen_group[gk] != r.Filename:
                flag = 'restated_in_other_document'
        if flag:
            u.loc[idx, 'dedupe_flag'] = flag
            continue
        if r._uid is not None:
            seen_id.add(r._uid)
        if r._root is not None:
            seen_root[(r._root, r.qty)] = idx
        if r._ids is not None:
            seen_ids |= r._ids
        seen_desc.add(r._desc)
        if not np.isnan(r.unit_mw):
            seen_group.setdefault((r.kind, r.cls, r.qty, round(r.unit_mw, 1)), r.Filename)
            if r.qty >= 10:
                seen_doc_fleet.add((r.Filename, r.kind, r.qty, round(r.unit_mw, 2)))
            if not r.rating_from_hp:
                seen_kw.append((r.qty, r.unit_mw, r.kind))
    return u.drop(columns=['_uid', '_root', '_desc', '_completeness', '_hp', '_ids'])


# ----------------------------------------------------------------------------
# text signals
# ----------------------------------------------------------------------------
def load_text_signals(work: Path):
    th = work / 'text_hits2.parquet'
    hits = pd.read_parquet(th) if th.exists() else pd.DataFrame()
    ctx = {}
    sn = work / 'snips.txt'
    if sn.exists():
        rx = re.compile(r"^data/interim/(?:extracted_text|dc_generator_text)/(.*?)\.txt:(.*)$")
        tied, boil = Counter(), Counter()
        with open(sn, errors='ignore') as f:
            for line in f:
                m = rx.match(line.rstrip('\n'))
                if not m:
                    continue
                d, s = m.groups()
                if TIED.search(s):
                    tied[d] += 1
                elif BOILER.search(s):
                    boil[d] += 1
        ctx = {'tied': tied, 'boiler': boil}
    return hits, ctx


# ----------------------------------------------------------------------------
# geo helpers
# ----------------------------------------------------------------------------
COORD_RX = [
    re.compile(r"lat(?:itude)?[^0-9\-\n]{0,25}(-?\d{2}\.\d{3,})[^0-9\-\n]{0,40}lon(?:gitude)?[^0-9\-\n]{0,25}(-?\d{2,3}\.\d{3,})", re.I),
    re.compile(r"(\d{2})[°º]\s*(\d{1,2})['′]\s*(\d{1,2}(?:\.\d+)?)?[\"″]?\s*N?[^0-9\n]{0,30}(\d{2,3})[°º]\s*(\d{1,2})['′]\s*(\d{1,2}(?:\.\d+)?)?[\"″]?\s*W?", re.I),
]


def coords_from_text(doc):
    for d in TEXT_DIRS:
        p = d / f"{doc}.txt"
        if p.exists():
            try:
                t = p.read_text(errors='ignore')[:400000]
            except Exception:
                return None
            m = COORD_RX[0].search(t)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
                if lon > 0:
                    lon = -lon
                return lat, lon
            m = COORD_RX[1].search(t)
            if m:
                lat = int(m.group(1)) + int(m.group(2)) / 60 + (float(m.group(3) or 0)) / 3600
                lon = -(int(m.group(4)) + int(m.group(5)) / 60 + (float(m.group(6) or 0)) / 3600)
                return lat, lon
            return None
    return None


def load_geo():
    cities = pd.read_parquet(CITY_CENTROIDS) if CITY_CENTROIDS.exists() else None
    counties, bbox = None, {}
    if COUNTY_SHP.exists():
        try:
            import geopandas as gpd
            g = gpd.read_file(COUNTY_SHP)
            g['state'] = g.STATEFP.map(FIPS)
            g['cname'] = g.NAME.str.lower().str.replace(r"[^a-z ]", "", regex=True).str.strip()
            gp = g.to_crs(5070)
            g['clat'] = gp.geometry.centroid.to_crs(4326).y
            g['clon'] = gp.geometry.centroid.to_crs(4326).x
            counties = g[['state', 'cname', 'clat', 'clon']]
            for st, gg in g.groupby('state'):
                b = gg.total_bounds
                bbox[st] = (b[1] - 0.3, b[3] + 0.3, b[0] - 0.3, b[2] + 0.3)
        except Exception as e:  # noqa: BLE001
            print('county shapefile unavailable:', e)
    return cities, counties, bbox


def in_bbox(lat, lon, st, bbox):
    if st not in bbox:
        return True
    a, b, c, d = bbox[st]
    return a <= lat <= b and c <= lon <= d


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--union', type=Path, required=True)
    ap.add_argument('--work-dir', type=Path, default=REPO / 'data/processed/analysis/dc_screen/work')
    ap.add_argument('--prior-sites', type=Path, default=None)
    ap.add_argument('--out', type=Path, default=REPO / 'data/processed/analysis/dc_generation_sites.csv')
    ap.add_argument('--units-out', type=Path, default=None)
    args = ap.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)

    df = load_union(args.union, args.work_dir)
    df = df[df.Status.fillna('').str.startswith('Success')]
    print(f"union rows {len(df):,} docs {df.Filename.nunique():,}")

    # ---------------- document table
    doc = df.groupby('Filename').agg(
        state=('Facility State Abbreviation', 'first'), facility=('Facility Name', 'first'),
        owner=('Owner/Operator Name', 'first'), address=('Facility Address', 'first'), city=('Facility City', 'first'),
        county=('Facility County', 'first'), naics=('NAICS Code', 'first'), naics_cls=('Classified NAICS', 'first'),
        naics_res=('NAICS Resolved', 'first'), sic=('SIC Code', 'first'), industry=('Industry Description', 'first'),
        permit=('Permit Number', 'first'), issued=('Issuance Date', 'first'), source_run=('Source Run', 'first'))
    for c in ('state', 'facility', 'owner', 'city'):
        doc[c] = doc[c].where(doc[c].fillna('').str.upper() != 'ERROR', None)
    doc['state'] = doc.state.fillna('').str.upper().str.strip()
    doc.loc[doc.index.str.contains('_DC_Permit|_TV_Permit') & ~doc.state.isin(['VA']), 'state'] = 'VA'
    doc.loc[doc.index.str.startswith('tx_') & (doc.state == ''), 'state'] = 'TX'
    for pre, st in (('az_', 'AZ'), ('nv_', 'NV'), ('or_', 'OR'), ('ga_', 'GA'), ('ms_', 'MS'), ('tn_', 'TN'), ('nc_', 'NC'), ('md_', 'MD'),
                    ('wa_', 'WA'), ('ia_', 'IA'), ('oh_', 'OH'), ('wi_', 'WI'), ('nj_', 'NJ'), ('il_', 'IL')):
        doc.loc[doc.index.str.startswith(pre) & (doc.state == ''), 'state'] = st
    blank = doc.state == ''
    for rx, st in ((r"\b\d{10}\b.*AIR PERMIT|AIR PERMIT.*\b\d{10}\b|tv\.sob", 'OH'), (r"^\d{4}-\d{2}$", 'MT'), (r"^[A-Za-z]{8}$", 'NM'), (r"^83\d{6}$", 'IN'),
                   (r"^\d+sop$|^\d+sob$|^permit-\d", 'TX'), (r"^A\d{6}[A-Z]_", 'AL'), (r"^\d+\[\d\d-\d+\]", 'PA'), (r"^AI\d+_", 'LA'), (r"^il_", 'IL')):
        doc.loc[blank & doc.index.str.contains(rx, regex=True), 'state'] = st
    nm = (doc.facility.fillna('') + ' | ' + doc.owner.fillna('') + ' | ' + doc.industry.fillna(''))
    doc['name_says_datacenter'] = nm.str.contains(DC_NAME)
    doc['operator_hit'] = nm.str.extract('(' + OPERATOR.pattern + ')', flags=re.I)[0]
    ncode = (doc.naics.fillna('') + ' ' + doc.naics_cls.fillna('') + ' ' + doc.naics_res.fillna(''))
    doc['naics_5182'] = ncode.str.contains(r"\b5182\d*|\b51821|\b54151\d|\b5191\d") | doc.sic.fillna('').str.contains(r"\b7374\b|\b7379\b")
    doc['from_dc_scrape_lane'] = (doc.source_run == 'dc_generators_2026-08') | doc.index.str.contains('_DC_Permit')
    doc['institutional'] = nm.str.contains(INSTITUTIONAL) & ~doc.name_says_datacenter & doc.operator_hit.isna() & ~doc.from_dc_scrape_lane

    hits, ctx = load_text_signals(args.work_dir)
    for col in ('dc_strict', 'btm_strict', 'naics', 'crypto'):
        doc['tx_' + col] = doc.index.map(hits[col]).fillna(0).astype(int) if col in hits.columns else 0
    doc['project_tied_hits'] = doc.index.map(lambda d: ctx.get('tied', {}).get(d, 0) if ctx else 0)
    doc['boilerplate_hits'] = doc.index.map(lambda d: ctx.get('boiler', {}).get(d, 0) if ctx else 0)

    # generator signature per doc
    units = unit_rows(df)
    gen_per_doc = units.groupby('Filename').qty.sum()
    doc['gen_units'] = doc.index.map(gen_per_doc).fillna(0).astype(int)

    prior = pd.read_csv(args.prior_sites) if args.prior_sites and args.prior_sites.exists() else None
    prior_doc_tier = {}
    if prior is not None:
        for _, r in prior.iterrows():
            for d in str(r.source_files).split(';'):
                d = d.strip()
                if d and d != 'nan':
                    prior_doc_tier[d] = r.evidence_tier
    doc['prior_tier'] = doc.index.map(prior_doc_tier)
    doc.loc[doc.prior_tier.notna(), 'institutional'] = False

    # ---------------- evidence tier per document
    strong_text = (doc.project_tied_hits >= 2) & (doc.boilerplate_hits == 0) & (doc.tx_dc_strict >= 3)
    tierA = (doc.name_says_datacenter | doc.naics_5182 | (doc.operator_hit.notna() & (doc.gen_units > 0))
             | (doc.from_dc_scrape_lane & (doc.name_says_datacenter | doc.naics_5182 | (doc.tx_dc_strict >= 1) | doc.operator_hit.notna()))
             | strong_text | doc.prior_tier.fillna('').str.startswith('A'))
    tierB = ((doc.project_tied_hits >= 1) & (doc.gen_units > 0)) | (doc.from_dc_scrape_lane & (doc.gen_units > 0)) | doc.prior_tier.fillna('').str.startswith('B')
    tierC = (doc.tx_dc_strict >= 2) | doc.prior_tier.fillna('').str.startswith('C') | doc.from_dc_scrape_lane
    excluded = nm.str.contains(EXCLUDE_NAMES)
    doc['doc_tier'] = np.select([tierA & ~doc.institutional & ~excluded, tierB & ~doc.institutional & ~excluded, tierC & ~doc.institutional & ~excluded],
                                ['A confirmed', 'B likely', 'C unverified'], None)
    doc.to_parquet(args.work_dir / 'sites_doc_table.parquet')
    dc_docs = doc[doc.doc_tier.notna()].copy()
    print(f"DC documents: {len(dc_docs):,} ({dc_docs.doc_tier.value_counts().to_dict()})")

    # ---------------- facility clustering (union-find)
    parent = {d: d for d in dc_docs.index}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    keys = defaultdict(list)
    tceq = {}
    if TCEQ_INDEX.exists():
        ti = pd.read_csv(TCEQ_INDEX, dtype=str)
        tceq = dict(zip(ti.dDocName, ti.xPrimaryID))
    def dirtok(name):
        m = re.search(r"\b(east|west|north|south)\b", str(name).lower())
        return m.group(1) if m else ''
    for d, r in dc_docs.iterrows():
        st = r.state
        dq = dirtok(r.facility)   # 'East Microgrid' and 'West Microgrid' at one address are two plants
        m = re.search(r"^tx_(?:.*?_)?(\d{6,8})_", d)
        if m and str(tceq.get(m.group(1), '')).isdigit():
            keys[f"rn:{tceq[m.group(1)]}"].append(d)
        m = re.match(r"^(az_F\d+|nv_A\d+[A-Z]?|or_\d+-\d+|ga_\d+-\d+)", d)
        if m:
            keys[f"lane:{m.group(1)}"].append(d)
        m = re.search(r"\b(\d{10})\b", d)
        if m and st == 'OH':
            keys[f"ohid:{m.group(1)}"].append(d)
        ak = addr_key(r.address)
        if ak and st:
            keys[f"addr:{st}|{ak}|{dq}"].append(d)
        for p in permits_of(r.permit):
            if st and len(re.findall(r"\d", p)) >= 3 and not re.search(r"cfr|part |subpart|section|title v|nsps|neshap|\bpsd\b|rule", p):
                keys[f"permit:{st}|{p}|{dq}"].append(d)
        cn = city_norm(r.city)
        toks = tokens(f"{r.facility or ''} {r.owner or ''}") - GENERIC_NAME_TOKENS - set(cn.split())
        if st and cn:
            for t in toks:
                if len(t) >= 4:
                    keys[f"cn:{st}|{cn}|{t}|{dq}"].append(d)
            # campus codes (CMH116, IAD-61, PDX-63, VA2, TX3 ...) as keys within a city
            for pre, n in re.findall(r"\b([a-z]{2,3})-? ?(\d{1,3})\b", f"{r.facility or ''} {r.owner or ''}".lower()):
                if pre in CID_PREFIXES:
                    keys[f"cid:{st}|{cn}|{pre}{n}"].append(d)
    # docs with neither city nor address: state-level key on rare name tokens (<=3 docs share the token)
    tokc = Counter()
    doc_toks = {}
    for d, r in dc_docs.iterrows():
        tk = tokens(f"{r.facility or ''} {r.owner or ''}") - GENERIC_NAME_TOKENS
        doc_toks[d] = tk
        for t in tk:
            tokc[(r.state, t)] += 1
    for d, r in dc_docs.iterrows():
        if r.state and not city_norm(r.city) and not addr_key(r.address):
            for t in doc_toks[d]:
                if len(t) >= 5 and tokc[(r.state, t)] <= 12:
                    keys[f"rare:{r.state}|{t}|{dirtok(r.facility)}"].append(d)
    if prior is not None:
        for _, r in prior.iterrows():
            ds = [x.strip() for x in str(r.source_files).split(';') if x.strip() in parent]
            for a, b in zip(ds, ds[1:]):
                union(a, b)
    for k, ds in keys.items():
        for a, b in zip(ds, ds[1:]):
            union(a, b)
    dc_docs['site_root'] = [find(d) for d in dc_docs.index]
    print(f"facilities: {dc_docs.site_root.nunique():,}")

    # ---------------- unit rollup per facility
    units = units[units.Filename.isin(dc_docs.index)].copy()
    units['site_root'] = units.Filename.map(dc_docs.site_root)
    units['doc_tier'] = units.Filename.map(dc_docs.doc_tier)
    kept = []
    for root, g in units.groupby('site_root'):
        kept.append(dedupe_units(g))
    units_d = pd.concat(kept) if kept else units.iloc[0:0]
    units_d['counted'] = units_d.unit_mw.notna() & (units_d.dedupe_flag == '')
    # A turbine fleet described in several documents of the SAME permit (successive amendments /
    # application versions, alternative equipment options, SC/CC restatements) -> keep only the latest
    # document's fleet. Documents with different permit numbers are treated as separate fleets.
    units_d['_date'] = pd.to_datetime(units_d['Issuance Date'], errors='coerce')
    doc_permits = dc_docs.permit.map(permits_of)
    for root, g in units_d[(units_d.kind == 'turbine') & units_d.counted].groupby('site_root'):
        docs_ = list(g.Filename.unique())
        if len(docs_) < 2:
            continue
        # union-find over shared permit numbers; docs without a permit number share one bucket
        par = {d: d for d in docs_}
        def f_(x):
            while par[x] != x:
                par[x] = par[par[x]]
                x = par[x]
            return x
        by_p = defaultdict(list)
        for d in docs_:
            ps = {p for p in doc_permits.get(d, set()) if re.search(r"\d{3}", p)} or {'(none)'}
            for p in ps:
                by_p[p].append(d)
        for p, ds in by_p.items():
            for x_, y_ in zip(ds, ds[1:]):
                par[f_(y_)] = f_(x_)
        groups = defaultdict(list)
        for d in docs_:
            groups[f_(d)].append(d)
        real = [p for p in by_p if p != '(none)']
        if len({f_(by_p[p][0]) for p in real}) <= 1:   # one permit family (+ TBD/none docs) -> one fleet
            groups = {'all': docs_}
        per_doc = g.groupby('Filename').agg(mw=('group_mw', 'sum'), date=('_date', 'max'))
        for members in groups.values():
            if len(members) < 2:
                continue
            keep = per_doc.loc[members].sort_values(['date', 'mw'], ascending=[False, False], na_position='last').index[0]
            mask = (units_d.site_root == root) & (units_d.kind == 'turbine') & units_d.Filename.isin([m for m in members if m != keep])
            units_d.loc[mask, 'counted'] = False
            units_d.loc[mask, 'dedupe_flag'] = 'fleet_superseded_in_same_permit'
    units_d = units_d.drop(columns=['_date'])
    # Backup-genset fleets are restated in every revision of a facility's permit (applications,
    # evaluations, renewals). Within one document family (same lane facility id, else same permit
    # number) count only the document with the largest backup fleet; families add up.
    def family(d):
        for rx in (r"^ia_([\d-]+)_", r"AQ_F_(\d+)", r"^il_dw_(\w+?)_", r"^(az_F\d+|nv_A\d+[A-Z]?|or_\d+-\d+|ga_\d+-\d+)", r"\b(\d{10})\b", r"^tx_(?:.*?_)?(\d{6,8})_", r"^(\d{5,7})_(?:DC|TV)_Permit"):
            m = re.search(rx, d)
            if m:
                key = m.group(1)
                if rx.startswith('^tx_') and str(tceq.get(key, '')).isdigit():
                    key = 'rn' + tceq[key]
                return 'fam:' + key
        ps = sorted(p for p in permits_of(dc_docs.permit.get(d)) if re.search(r"\d{3}", p))
        return 'permit:' + '|'.join(ps) if ps else 'doc:' + d
    units_d['_family'] = units_d.Filename.map(family)
    for (root, fam), g in units_d[(units_d.cls == 'backup/standby') & units_d.counted].groupby(['site_root', '_family']):
        if g.Filename.nunique() < 2:
            continue
        per_doc = g.groupby('Filename').group_mw.sum()
        keep = per_doc.idxmax()
        mask = (units_d.site_root == root) & (units_d._family == fam) & (units_d.cls == 'backup/standby') & (units_d.Filename != keep)
        units_d.loc[mask, 'counted'] = False
        units_d.loc[mask, 'dedupe_flag'] = 'backup_fleet_restated_in_family'
    units_d = units_d.drop(columns=['_family'])

    # est missing MW where a model is named
    def est_model(row):
        s = f"{row['Unit Make']} {row['Unit Model']} {row['Unit Description']}".lower()
        for pat, mw, kind in MODEL_RATINGS:
            if re.search(pat, s):
                if kind == 'turbine' and row.kind != 'turbine':
                    continue
                if kind == 'engine' and row.kind == 'turbine':
                    continue
                return pat, mw
        return None, np.nan
    em = [est_model(r) for _, r in units_d.iterrows()]
    units_d['est_model'] = [x[0] for x in em]
    units_d['est_mw_nom'] = [x[1] for x in em]
    units_d['est_missing_group_mw'] = np.where(~units_d.counted & units_d.est_mw_nom.notna(), units_d.est_mw_nom * units_d.qty, np.nan)

    rows = []
    cities, counties, bbox = load_geo()
    prior_by_doc = {}
    if prior is not None:
        for i, r in prior.iterrows():
            for d in str(r.source_files).split(';'):
                prior_by_doc[d.strip()] = i
    for root, g in dc_docs.groupby('site_root'):
        u = units_d[units_d.site_root == root]
        best = g.sort_values(['doc_tier', 'gen_units'], ascending=[True, False]).iloc[0]
        namecands = g.facility.dropna()
        fac = namecands.value_counts().index[0] if len(namecands) else '(unnamed)'
        owner = g.owner.dropna().value_counts().index[0] if g.owner.notna().any() else None
        st = best.state
        city = g.city.dropna().value_counts().index[0] if g.city.notna().any() else None
        county = g.county.dropna().value_counts().index[0] if g.county.notna().any() else None
        addr = g.address.dropna().value_counts().index[0] if g.address.notna().any() else None
        tier = sorted(g.doc_tier)[0]
        counted = u[u.counted]
        total = counted.group_mw.sum()
        prime = counted[counted.cls == 'prime/grid'].group_mw.sum()
        backup = counted[counted.cls == 'backup/standby'].group_mw.sum()
        turb = counted[counted.kind == 'turbine'].group_mw.sum()
        eng = counted[counted.kind != 'turbine'].group_mw.sum()
        n_units = int(counted.qty.sum())
        unc = u[~u.counted & (u.dedupe_flag == '')]
        prior_idx = next((prior_by_doc[d] for d in g.index if d in prior_by_doc), None)
        pr = prior.loc[prior_idx] if prior_idx is not None else None
        lat = lon = np.nan
        geo_src = geo_prec = None
        if pr is not None and pd.notna(pr.get('lat')):
            lat, lon, geo_src, geo_prec = pr.lat, pr.lon, pr.geo_source, pr.geo_precision
        else:
            for d in g.sort_values('gen_units', ascending=False).index:
                c = coords_from_text(d)
                if c and in_bbox(c[0], c[1], st, bbox) and abs(c[0]) > 1:
                    lat, lon, geo_src, geo_prec = c[0], c[1], 'permit text', 'facility'
                    break
            if np.isnan(lat) and cities is not None and city:
                h = cities[(cities.key_state == st) & (cities.key_city == city_norm(city))]
                if len(h):
                    lat, lon, geo_src, geo_prec = float(h.Lat.iloc[0]), float(h.Lon.iloc[0]), 'city centroid', 'city (~km)'
            if np.isnan(lat) and counties is not None and county:
                cn = re.sub(r"[^a-z ]", "", str(county).lower().replace('county', '').replace('parish', '')).strip()
                h = counties[(counties.state == st) & (counties.cname == cn)]
                if len(h):
                    lat, lon, geo_src, geo_prec = float(h.clat.iloc[0]), float(h.clon.iloc[0]), 'county centroid', 'county (~10s km)'
        if np.isnan(lat):
            geo_src, geo_prec = 'none', 'none'
        issued = pd.to_datetime(g.issued, errors='coerce')
        rows.append({
            'site_id': root, 'state': st, 'facility_name': fac, 'city': city, 'county': county, 'lat': lat, 'lon': lon,
            'geo_source': geo_src, 'geo_precision': geo_prec, 'address': addr, 'owner_operator': owner,
            'total_mw': round(total, 1), 'prime_grid_mw': round(prime, 1), 'backup_standby_mw': round(backup, 1),
            'turbine_mw': round(turb, 1), 'engine_mw': round(eng, 1), 'n_units': n_units,
            'largest_unit_mw': round(float(counted.unit_mw.max()), 2) if len(counted) else np.nan,
            'uncounted_units': int(unc.qty.sum()), 'uncounted_unit_groups': int(len(unc)),
            'capacity_completeness': round(n_units / (n_units + unc.qty.sum()), 3) if (n_units + unc.qty.sum()) else np.nan,
            'est_missing_mw_nom': round(float(unc.est_missing_group_mw.sum()), 1) if unc.est_missing_group_mw.notna().any() else 0.0,
            'est_missing_models': '; '.join(sorted({str(m) for m in unc.est_model.dropna()})) or None,
            'uncounted_units_no_model': int(unc[unc.est_model.isna()].qty.sum()),
            'site_mw_upper_estimate': round(total + (float(unc.est_missing_group_mw.sum()) if unc.est_missing_group_mw.notna().any() else 0.0), 1),
            'fuel_types': '; '.join(sorted({str(x).lower() for x in u['Fuel Type'].dropna()}))[:120] or None,
            'n_documents': len(g), 'permit_numbers': '; '.join(sorted({str(p) for p in g.permit.dropna()}))[:200] or None,
            'first_issued': issued.min().date().isoformat() if issued.notna().any() else None,
            'last_issued': issued.max().date().isoformat() if issued.notna().any() else None,
            'evidence_tier': tier, 'dc_text_mentions': int(g.tx_dc_strict.sum()),
            'project_tied_text': bool((g.project_tied_hits > 0).any()), 'boilerplate_hits': int(g.boilerplate_hits.sum()),
            'name_says_datacenter': bool(g.name_says_datacenter.any()), 'naics_5182': bool(g.naics_5182.any()),
            'from_dc_scrape_lane': bool(g.from_dc_scrape_lane.any()), 'operator_hit': '; '.join(sorted({str(x) for x in g.operator_hit.dropna()}))[:80] or None,
            'naics': g.naics.dropna().iloc[0] if g.naics.notna().any() else None,
            'prior_site': pr.facility_name if pr is not None else None,
            'screen_added': pr is None,
            'source_files': '; '.join(sorted(g.index)),
        })
    sites = pd.DataFrame(rows)
    # identical fleets that escaped clustering (same state, unit count and MW, a shared name token) -> one site
    sites['_k'] = sites.state.fillna('') + '|' + sites.n_units.astype(str) + '|' + sites.total_mw.round(0).astype(str)
    merged_rows, drop = [], set()
    for k, g in sites[sites.n_units > 0].groupby('_k'):
        if len(g) < 2:
            continue
        base = g.index[0]
        for other in g.index[1:]:
            if tokens(sites.at[base, 'facility_name']) & tokens(sites.at[other, 'facility_name']):
                sites.at[base, 'n_documents'] += sites.at[other, 'n_documents']
                sites.at[base, 'source_files'] = '; '.join(sorted(set(sites.at[base, 'source_files'].split('; ')) | set(sites.at[other, 'source_files'].split('; '))))
                for c in ('city', 'county', 'address', 'owner_operator', 'prior_site'):
                    if pd.isna(sites.at[base, c]) and pd.notna(sites.at[other, c]):
                        sites.at[base, c] = sites.at[other, c]
                if pd.isna(sites.at[base, 'lat']) and pd.notna(sites.at[other, 'lat']):
                    for c in ('lat', 'lon', 'geo_source', 'geo_precision'):
                        sites.at[base, c] = sites.at[other, c]
                sites.at[base, 'screen_added'] = bool(sites.at[base, 'screen_added'] and sites.at[other, 'screen_added'])
                drop.add(other)
    if drop:
        print(f"merged {len(drop)} duplicate-fleet sites")
        sites = sites.drop(index=list(drop))
    sites = sites.drop(columns=['_k']).sort_values(['evidence_tier', 'total_mw'], ascending=[True, False])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sites.to_csv(args.out, index=False)
    uo = args.units_out or args.out.with_name(args.out.stem.replace('sites', 'units') + '.csv')
    ucols = ['site_root', 'Filename', 'doc_tier', 'Unit ID', 'Unit Description', 'Unit Quantity', 'qty', 'Unit Make', 'Unit Model',
             'Unit Type', 'Fuel Type', 'Capacity Value', 'Capacity Unit', 'Generation Capacity', 'kind', 'cls', 'elec_mw', 'hp_mw', 'heat_mmbtu', 'unit_mw', 'group_mw', 'rating_src',
             'rating_from_hp', 'capacity_flag', 'dedupe_flag', 'counted', 'est_model', 'est_mw_nom', 'est_missing_group_mw', 'Permit Number', 'Issuance Date']
    units_d[ucols].rename(columns={'site_root': 'site_id'}).to_csv(uo, index=False)

    # ---------------- summary
    a = sites[sites.evidence_tier.str.startswith('A')]
    print(f"\nsites {len(sites):,} (A {len(a):,} / B {(sites.evidence_tier.str.startswith('B')).sum():,} / C {(sites.evidence_tier.str.startswith('C')).sum():,}); "
          f"tier-A MW {a.total_mw.sum():,.0f} (prime {a.prime_grid_mw.sum():,.0f} / backup {a.backup_standby_mw.sum():,.0f}); "
          f"new vs prior: {int(sites.screen_added.sum())} sites")
    print(a.groupby('state').agg(sites=('site_id', 'size'), mw=('total_mw', 'sum'), prime=('prime_grid_mw', 'sum'), backup=('backup_standby_mw', 'sum')).sort_values('mw', ascending=False).head(15).round(0).to_string())
    print(f"\nwrote {args.out} and {uo}")
    if prior is not None:
        covered = set()
        for sf in sites.source_files:
            covered.update(x.strip() for x in sf.split(';'))
        lost = prior[prior.evidence_tier.str.startswith('A') & ~prior.source_files.fillna('').map(lambda v: any(x.strip() in covered for x in v.split(';')))]
        print(f"prior tier-A sites with no document in the new table: {len(lost)} (e.g. {lost.facility_name.head(8).tolist()})")
        merged = sites.prior_site.notna().sum()
        print(f"new sites carrying a prior site: {merged}; prior tier-A sites: {int(prior.evidence_tier.str.startswith('A').sum())}")


if __name__ == '__main__':
    main()
