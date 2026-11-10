#!/usr/bin/env python3
"""Build the de-identified public release from the union CSV, and the matching
de-identified dashboard parquet.

The public release keeps county, state and industry codes as the only facility
descriptors. Removed: Facility Name, Owner/Operator Name, Facility Address,
Facility City, Facility Zip Code, Permit Number, Filename (it often carries the
facility or permit name, e.g. "174655 Tesoro-C Rev. 73") and the empty Spec
Sheet Link. Added in their place:

  * ``Document ID``  opaque key for the source permit document (replaces
        Filename); ``Latest Facility Filename`` becomes ``Latest Facility
        Document ID`` with the same mapping
  * ``Facility ID``  opaque key for the facility, from the recipe the paper
        uses to count facilities (scripts/manuscript_mfg_subset_stats.py:
        standardized name + state + ZIP, city when ZIP is blank); blank when
        the permit gives no facility name

Both IDs are keyed HMAC-SHA256 digests, so they are stable across rebuilds and
cannot be reversed without the key in data/private/deid_key.hex (gitignored,
with the ID -> filename / facility maps beside it).

Free text: each row's own facility name, owner name and street address are
replaced with "[facility]", "[owner]" and "[address]" in every free-text column
(Industry Description, Unit Description, Emission Limits, ...). Codes, dates,
equipment categories, Unit Make/Model and Regulatory Authority are left alone.
Names are matched as whole phrases after legal suffixes (Inc, LLC, ...) are
stripped; a phrase made only of generic words ("Boiler Plant") is skipped so
equipment text is not blanked, and a distinctive lead ("Willow Island" of
"Willow Island Power Station") is matched too. This removes names, it does not
anonymize: county + six-digit NAICS can still point to a single plant.

The combustion flag column is named ``Equipment Designation`` (formerly
Combustion Source / Equipment Type).

Release outputs (--out-dir):
    permit_equipment_full.csv.gz            every processed permit
    permit_equipment_manufacturing.csv.gz   NAICS Resolved in 31-33
    permit_equipment_full.parquet           same as the full CSV (for GCS)
    per_state_full.zip, per_state_manufacturing.zip
    data_dictionary.csv, manifest.json (rows, documents, facilities, SHA-256)
Private outputs (data/private/): deid_documents.csv, deid_facilities.csv,
    deid_scrub_samples.csv (before/after examples, for review)

Dashboard mode (--dashboard-in/--dashboard-out) applies the same transform to
a dashboard-ready parquet (scripts/build_dashboard_ready_parquet.py output):
Site Key becomes the Facility ID (Document ID when there is none), key_city
becomes key_county, and the identifying columns are dropped.

Usage (from the repo root):
    PYTHONPATH=dashboards python scripts/deidentify_release.py \
        --union data/processed/permit_data_union_v5g.csv \
        --out-dir data/processed/release/v5g_deid \
        --dashboard-in data/processed/dashboard/permits_v5g_ready.parquet \
        --dashboard-out data/processed/dashboard/permits_v5g_deid_ready.parquet
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import hmac
import json
import re
import secrets
import shutil
import sys
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "scripts"), str(ROOT / "dashboards")):
    if p not in sys.path:
        sys.path.insert(0, p)
from manuscript_mfg_subset_stats import STATES, norm_name, zip5  # noqa: E402
from manufacturing_subsector import norm_county  # noqa: E402

PRIVATE = ROOT / "data/private"
KEY_PATH = PRIVATE / "deid_key.hex"
MANUSCRIPT = ROOT / "docs/docs/nature_data_paper_manuscript.md"
MFG_SECTORS = ("31", "32", "33")
BATCH_MB = 32

# release (short) and dashboard (long) names of the identifying fields
C_FN = "Filename"
C_NAME = "Facility Name"
C_OWNER = ("Owner/Operator Name", "Owner/Operator Name (if different from Facility Name)")
C_ADDR, C_CITY, C_STATE, C_ZIP = "Facility Address", "Facility City", "Facility State Abbreviation", "Facility Zip Code"
C_LFF = "Latest Facility Filename"
DROP = {C_FN, C_NAME, *C_OWNER, C_ADDR, C_CITY, C_ZIP, "Permit Number", "Spec Sheet Link",
        C_LFF, "Facility City (norm)", "key_city"}
RENAME = {"Combustion Source": "Equipment Designation", "Equipment Type": "Equipment Designation"}
# columns never scrubbed: codes, dates, numbers, categories, provenance, and
# fields where a facility-name match would be a false positive (makes, agencies)
NO_SCRUB = {
    "Status", "Processing Date", "Model Used", "Model Used (AI extraction model)", C_STATE,
    "Facility County", "Facility County (norm)", "NAICS Code", "SIC Code",
    "SIC Code (Standard Industrial Classification)", "Classified NAICS", "NAICS Resolved",
    "NAICS Source", "NAICS_clean", "Subsector", "Operating Hours — numeric/value portion",
    "Annual Run Hours — numeric/value portion", "Unit Quantity", "Unit Quantity (num)",
    "Year of Manufacture", "Capacity Value", "Capacity Value (num)", "Capacity Unit",
    "Capacity Unit (norm)", "Unit Make", "Unit Make (norm)", "Unit Model", "Issuance Date",
    "Expiration Date", "Regulatory Authority", "Regulatory Authority (norm)", "Permit Type",
    "Permit Type (e.g. Title V, State Only, Synthetic Minor)", "Duplicate Equipment Documents",
    "Source Run", "Source Sheet", "Pieces Failed", "Equipment Category", "Equipment Subcategory",
    "Combustion Source", "Equipment Type", "Equipment Designation", "Equipment Category Basis",
    "Site Key", "key_state", "key_county", "Document ID", "Facility ID",
    "Latest Facility Document ID", "Fuel Type (norm)",
}

LEGAL = {"inc", "incorporated", "llc", "l", "c", "lp", "llp", "ltd", "limited", "co", "corp",
         "corporation", "company", "plc", "usa", "us", "the"}
GENERIC = {
    "plant", "plants", "facility", "facilities", "mill", "mills", "station", "refinery",
    "refining", "power", "works", "terminal", "complex", "site", "division", "operations",
    "operation", "company", "corporation", "energy", "products", "product", "manufacturing",
    "industries", "industrial", "center", "centre", "of", "and", "at", "boiler", "unit",
    "units", "generating", "production", "compressor", "gas", "oil", "chemical", "chemicals",
    "paper", "steel", "cement", "food", "foods", "services", "systems", "group", "holdings",
    "international", "american", "national", "north", "south", "east", "west", "county",
    "city", "landfill", "hospital", "university", "the", "new", "plastics", "metals",
    "packaging", "materials", "process", "processing", "storage", "&",
}
TOKEN_RX = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?|&")
SEP = r"[\s\-_.,/']{0,3}"
SAME_AS = {"", "same", "same as facility", "same as above", "n a", "na", "none", "not provided",
           "not specified", "unknown"}
# extraction placeholders that land in the name fields of failed or empty rows
PLACEHOLDER = SAME_AS | {"error", "null", "nan", "tbd", "not stated", "not available",
                         "not listed", "not applicable", "see attached", "various"}
# a candidate phrase that occurs in the equipment vocabulary (Unit Type, Fuel Type,
# Control Device(s), Pollutants) of at least this many documents is a description,
# not a name ("Natural Gas", "Pump Station", "Hot Mix Asphalt Plant")
VOCAB_COLS = ["Unit Type", "Fuel Type", "Control Device(s)", "Pollutants"]
VOCAB_MIN_DOCS = 25


def load_key() -> bytes:
    PRIVATE.mkdir(parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        KEY_PATH.write_text(secrets.token_hex(32))
        KEY_PATH.chmod(0o600)
        print(f"created new ID key {KEY_PATH}")
    return bytes.fromhex(KEY_PATH.read_text().strip())


def make_id(key: bytes, kind: str, value: str) -> str:
    digest = hmac.new(key, f"{kind}\0{value}".encode(), hashlib.sha256).hexdigest()
    return ("D" if kind == "doc" else "F") + digest[:10].upper()


def _tokens(s: str) -> list[str]:
    toks = TOKEN_RX.findall(s or "")
    while toks and toks[-1].lower().strip("'’") in LEGAL:
        toks.pop()
    while toks and toks[0].lower() == "the":
        toks.pop(0)
    return toks


def _name_candidates(s: str) -> list[list[str]]:
    """Name token lists from the bare name up to the full name with its legal
    suffixes ("Donaldson", "Donaldson Company", "Donaldson Company Inc"). The
    caller takes the shortest that is safe to match: a bare name that is too
    short ("NSK") or also an equipment make ("Donaldson", "Caterpillar") falls
    back to the name with its suffix."""
    raw = TOKEN_RX.findall(s or "")
    while raw and raw[0].lower() == "the":
        raw.pop(0)
    out = [raw]
    toks = list(raw)
    while toks and toks[-1].lower().strip("'’") in LEGAL:
        toks.pop()
        out.append(list(toks))
    return [c for c in reversed(out) if c]


def _alnum_len(toks: list[str]) -> int:
    return sum(len(t) for t in toks if t != "&")


def _phrase_ok(toks: list[str]) -> bool:
    words = [t for t in toks if t != "&"]
    if not words or all(t.lower() in GENERIC for t in words):
        return False
    # one-word names ("Cummins", "Nucor") count from five letters
    return _alnum_len(toks) >= (6 if len(words) >= 2 else 5)


def _rx(toks: list[str]) -> re.Pattern:
    body = SEP.join(re.escape(t) for t in toks)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _key(toks: list[str]) -> str:
    return " ".join(t.lower() for t in toks if t != "&")


def equipment_vocabulary(union: Path) -> set[str]:
    """1-4 word phrases used in the equipment fields of >= VOCAB_MIN_DOCS documents."""
    _, reader = _csv_reader(union, [C_FN] + VOCAB_COLS)
    docs_per_value: Counter = Counter()
    for batch in reader:
        df = batch.to_pandas()
        for c in VOCAB_COLS:
            v = df[[C_FN, c]].drop_duplicates()
            docs_per_value.update(v[c].str.lower().str.strip().value_counts().to_dict())
    ngram_docs: Counter = Counter()
    for value, n in docs_per_value.items():
        words = [t for t in TOKEN_RX.findall(value) if t != "&"][:12]
        seen = set()
        for k in range(1, 5):
            for i in range(len(words) - k + 1):
                seen.add(" ".join(words[i:i + k]))
        for g in seen:
            ngram_docs[g] += n
    vocab = {g for g, n in ngram_docs.items() if n >= VOCAB_MIN_DOCS}
    print(f"equipment vocabulary: {len(vocab):,} phrases in >= {VOCAB_MIN_DOCS} documents")
    return vocab


def build_scrubber(name: str, owner: str, addr: str, vocab: set[str] = frozenset()):
    """(prefilter substrings, [(regex, label)]) for one document, or None."""
    rules: list[tuple[list[str], str]] = []
    if _key(_tokens(name)) in PLACEHOLDER:
        name = ""
    if _key(_tokens(owner)) in PLACEHOLDER:
        owner = ""

    def ok(toks: list[str]) -> bool:
        return _phrase_ok(toks) and _key(toks) not in vocab

    ftoks = next((c for c in _name_candidates(name) if ok(c)), _tokens(name))
    if ok(ftoks):
        rules.append((ftoks, "[facility]"))
    # distinctive leads: the words before the first generic one ("Willow Island"
    # of "Willow Island Power Station") and the first two words ("Air Products"
    # of "Air Products and Chemicals"); never a single word, which is often a
    # city ("Detroit" of "Detroit Refinery" would hit "Detroit Diesel")
    lead = []
    for t in ftoks:
        if t.lower() in GENERIC:
            break
        lead.append(t)
    words = [t for t in ftoks if t != "&"]
    first2 = ftoks[:ftoks.index(words[1]) + 1] if len(words) >= 3 and words[0].lower() not in GENERIC else []
    for cand in (lead, first2):
        if cand and cand != ftoks and len([t for t in cand if t != "&"]) >= 2 and _alnum_len(cand) >= 8 \
                and ok(cand) and all(cand != r[0] for r in rules):
            rules.append((cand, "[facility]"))
    otoks = next((c for c in _name_candidates(owner) if ok(c)), [])
    if otoks and [t.lower() for t in otoks] != [t.lower() for t in ftoks] and ok(otoks):
        rules.append((otoks, "[owner]"))
    atoks = _tokens((addr or "").split(",")[0])
    if any(t.isdigit() for t in atoks) and len(atoks) >= 2 and _alnum_len(atoks) >= 8:
        rules.append((atoks, "[address]"))
    if not rules:
        return None
    # longest phrase first so a full name wins over its lead
    rules.sort(key=lambda r: -_alnum_len(r[0]))
    prefilter = tuple({next(t for t in toks if t != "&").lower() for toks, _ in rules})
    return prefilter, [(_rx(toks), label) for toks, label in rules]


def scrub_value(val: str, sc, matched: Counter | None = None) -> tuple[str, list[str]]:
    lv = val.lower()
    if not any(t in lv for t in sc[0]):
        return val, []
    hits = []
    for rx, label in sc[1]:
        if matched is not None:
            for m in rx.findall(val):
                matched[(label, m.lower())] += 1
        val, n = rx.subn(label, val)
        if n:
            hits.append(label)
    return val, hits


def _norm_text(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def _csv_reader(path: Path, cols: list[str] | None = None):
    with path.open(encoding="utf-8", newline="") as f:
        header = next(csv.reader(f))
    conv = pa_csv.ConvertOptions(column_types={c: pa.string() for c in header},
                                 strings_can_be_null=False,
                                 include_columns=cols or header)
    return header, pa_csv.open_csv(path, read_options=pa_csv.ReadOptions(block_size=BATCH_MB << 20),
                                   parse_options=pa_csv.ParseOptions(newlines_in_values=True),
                                   convert_options=conv)


class DocInfo:
    """Per-document IDs and scrubbers, from the first row of each document."""

    def __init__(self, union: Path, key: bytes):
        vocab = equipment_vocabulary(union)
        header, reader = _csv_reader(union, [C_FN, C_NAME, C_OWNER[0], C_ADDR, C_CITY, C_STATE, C_ZIP])
        first: dict[str, dict] = {}
        for batch in reader:
            for r in batch.to_pandas().drop_duplicates(C_FN).to_dict("records"):
                first.setdefault(r[C_FN], r)
        self.doc_id, self.fac_id, self.fac_key, self.scrubber = {}, {}, {}, {}
        for fn, r in first.items():
            name, state = r[C_NAME].strip(), r[C_STATE].strip()
            self.doc_id[fn] = make_id(key, "doc", fn)
            if name:
                fk = norm_name(name) + "|" + state + "|" + (zip5(r[C_ZIP].strip()) or r[C_CITY].strip().upper())
                self.fac_key[fn] = fk
                self.fac_id[fn] = make_id(key, "fac", fk)
            else:
                self.fac_id[fn] = ""
            self.scrubber[fn] = build_scrubber(name, r[C_OWNER[0]].strip(), r[C_ADDR].strip(), vocab)
        self.facility_name = {fn: first[fn][C_NAME].strip() for fn in first}
        assert len(set(self.doc_id.values())) == len(self.doc_id), "Document ID collision"
        n_fk = len(set(self.fac_key.values()))
        assert len({self.fac_id[fn] for fn in self.fac_key}) == n_fk, "Facility ID collision"
        stripped = Counter(fn.strip() for fn in first)
        self.ws_merges = sum(1 for n in stripped.values() if n > 1)
        self.by_stripped = {fn.strip(): fn for fn in first if stripped[fn.strip()] == 1}
        print(f"documents {len(first):,}; facility keys {n_fk:,}; documents with a scrubber "
              f"{sum(v is not None for v in self.scrubber.values()):,}; filenames equal after "
              f"stripping whitespace {self.ws_merges}")

    def lookup(self, fn: str) -> str | None:
        if fn in self.doc_id:
            return fn
        return self.by_stripped.get(fn.strip())

    def write_private_maps(self) -> None:
        with (PRIVATE / "deid_documents.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["filename", "document_id", "facility_id", "facility_key"])
            for fn in sorted(self.doc_id):
                w.writerow([fn, self.doc_id[fn], self.fac_id[fn], self.fac_key.get(fn, "")])
        facs = sorted({(self.fac_id[fn], k) for fn, k in self.fac_key.items()})
        with (PRIVATE / "deid_facilities.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["facility_id", "facility_key"])
            w.writerows(facs)


class ScrubLog:
    def __init__(self):
        self.cells = Counter()          # (column, label) -> cells changed
        self.matched = Counter()        # (label, matched text) -> cells
        self.samples: list[dict] = []
        self.per_col_samples = Counter()
        self.residual_checked = 0
        self.residual_hits = 0
        self.residual_examples: list[dict] = []


def transform(df: pd.DataFrame, info: DocInfo, log: ScrubLog, residual_every: int = 0) -> pd.DataFrame:
    """De-identify one batch (release or dashboard schema). Input columns are strings."""
    fns = [info.lookup(f) for f in df[C_FN].tolist()]
    missing = sum(f is None for f in fns)
    if missing:
        raise SystemExit(f"{missing} rows have a Filename not in the union CSV")
    df["Document ID"] = [info.doc_id[f] for f in fns]
    df["Facility ID"] = [info.fac_id[f] for f in fns]
    if C_LFF in df.columns:
        df["Latest Facility Document ID"] = [
            info.doc_id[k] if (k := info.lookup(v)) else "" for v in df[C_LFF].fillna("").astype(str)]
    scrub_cols = [c for c in df.columns if c not in NO_SCRUB and c not in DROP
                  and (pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c])
                       or isinstance(df[c].dtype, pd.CategoricalDtype))]
    scs = [info.scrubber[f] for f in fns]
    for c in scrub_cols:
        vals = df[c].astype(object).tolist()
        changed = False
        for i, (v, sc) in enumerate(zip(vals, scs)):
            if sc is None or not isinstance(v, str) or not v:
                continue
            nv, hits = scrub_value(v, sc, log.matched)
            if hits:
                vals[i] = nv
                changed = True
                for h in hits:
                    log.cells[(c, h)] += 1
                if log.per_col_samples[c] < 25:
                    log.per_col_samples[c] += 1
                    log.samples.append({"document": fns[i], "column": c, "before": v[:400], "after": nv[:400]})
        if changed:
            df[c] = vals
    if residual_every:
        for i in range(0, len(df), residual_every):
            name = _norm_text(info.facility_name[fns[i]])
            if len(name) < 8 or scs[i] is None:
                continue
            log.residual_checked += 1
            for c in scrub_cols:
                v = df[c].iat[i]
                if isinstance(v, str) and name in _norm_text(v):
                    log.residual_hits += 1
                    if len(log.residual_examples) < 50:
                        log.residual_examples.append({"document": fns[i], "column": c, "value": v[:300]})
                    break
    df = df.drop(columns=[c for c in df.columns if c in DROP]).rename(columns=RENAME)
    return df


# --------------------------------------------------------------------------- #
# release build
# --------------------------------------------------------------------------- #
def release_columns(header: list[str]) -> list[str]:
    out = ["Document ID", "Facility ID"]
    for c in header:
        if c == C_LFF:
            out.append("Latest Facility Document ID")
        elif c not in DROP:
            out.append(RENAME.get(c, c))
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def data_dictionary(cols: list[str], out: Path) -> None:
    """Descriptions from Table 1 of the manuscript, plus the new key columns."""
    desc: dict[str, tuple[str, str]] = {}
    in_table = False
    for line in MANUSCRIPT.read_text(encoding="utf-8").splitlines():
        if line.startswith("| Column | Description |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 3 and not set(cells[0]) <= {"-", ":"}:
                desc[cells[0]] = (cells[1], cells[2])
    desc["Document ID"] = ("Opaque identifier of the source permit document; all rows from one "
                           "document share it. Replaces the source filename, which can name the "
                           "facility or permit", "text")
    desc["Facility ID"] = ("Opaque identifier of the facility (standardized facility name + state + "
                           "ZIP code, city when the ZIP code is blank); blank when the permit names "
                           "no facility. Counts of distinct Facility IDs reproduce the facility "
                           "counts in the paper", "text")
    desc["Latest Facility Document ID"] = ("Document ID of the document kept for the facility in "
                                           "the deduplication (Section 2.3)", "text")
    desc["Equipment Designation"] = ("combustion, non-combustion, or unknown: whether the unit burns "
                                     "fuel (named Combustion Source in pre-release files)", "text")
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["column", "description", "type"])
        for c in cols:
            d, t = desc.get(c, ("", "text"))
            if not d:
                print(f"  WARNING: no description for {c!r}")
            w.writerow([c, d, t])


def build_release(union: Path, out_dir: Path, info: DocInfo, log: ScrubLog, label: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_per_state"
    if tmp.exists():
        shutil.rmtree(tmp)
    header, reader = _csv_reader(union)
    cols = release_columns(header)
    paths = {"full": out_dir / "permit_equipment_full.csv.gz",
             "manufacturing": out_dir / "permit_equipment_manufacturing.csv.gz"}
    gz = {k: gzip.open(p, "wt", encoding="utf-8", newline="", compresslevel=6) for k, p in paths.items()}
    pq_path = out_dir / "permit_equipment_full.parquet"
    schema = pa.schema([pa.field(c, pa.string()) for c in cols])
    pw = pq.ParquetWriter(pq_path, schema, compression="zstd", use_dictionary=True)
    state_fh: dict[tuple[str, str], object] = {}
    stats = {k: {"rows": 0, "documents": set(), "facilities": set(), "status": Counter()} for k in paths}
    first = True
    for n, batch in enumerate(reader, 1):
        df = transform(batch.to_pandas(), info, log, residual_every=20)[cols]
        mfg = df["NAICS Resolved"].str[:2].isin(MFG_SECTORS)
        st_ = df[C_STATE].str.strip().str.upper()
        st_ = st_.where(st_.isin(STATES), "unknown_state")
        for k, part in (("full", df), ("manufacturing", df[mfg])):
            part.to_csv(gz[k], index=False, header=first, lineterminator="\n")
            s = stats[k]
            s["rows"] += len(part)
            s["documents"].update(part["Document ID"])
            s["facilities"].update(x for x in part["Facility ID"] if x)
            s["status"].update(part["Status"].value_counts().to_dict())
            for state, g in part.groupby(st_[part.index]):
                fk = (k, state)
                if fk not in state_fh:
                    d = tmp / k
                    d.mkdir(parents=True, exist_ok=True)
                    state_fh[fk] = (d / f"{k}_{state}.csv").open("w", encoding="utf-8", newline="")
                    g.to_csv(state_fh[fk], index=False, lineterminator="\n")
                else:
                    g.to_csv(state_fh[fk], index=False, header=False, lineterminator="\n")
        pw.write_table(pa.Table.from_pandas(df, schema=schema, preserve_index=False))
        first = False
        print(f"  batch {n}: {stats['full']['rows']:,} rows written", flush=True)
    pw.close()
    for f in gz.values():
        f.close()
    for f in state_fh.values():
        f.close()
    for k in paths:
        zp = out_dir / f"per_state_{k}.zip"
        with zipfile.ZipFile(zp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted((tmp / k).glob("*.csv")):
                z.write(p, arcname=p.name)
    shutil.rmtree(tmp)
    data_dictionary(cols, out_dir / "data_dictionary.csv")

    tables = {k: {"rows": s["rows"], "documents": len(s["documents"]),
                  "facilities": len(s["facilities"]), "rows_by_status": dict(s["status"])}
              for k, s in stats.items()}
    files = []
    for p in sorted(out_dir.iterdir()):
        if p.is_file() and p.name != "manifest.json":
            files.append({"file": p.name, "bytes": p.stat().st_size, "sha256": _sha256(p)})
    manifest = {
        "release": label,
        "built": date.today().isoformat(),
        "source": union.name,
        "columns": cols,
        "tables": tables,
        "removed_columns": sorted(c for c in header if c in DROP),
        "free_text_scrub": {"cells_changed": sum(log.cells.values()),
                            "by_column": {f"{c} {lab}": n for (c, lab), n in sorted(log.cells.items())}},
        "files": files,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


# --------------------------------------------------------------------------- #
# dashboard parquet
# --------------------------------------------------------------------------- #
def build_dashboard(src: Path, dst: Path, info: DocInfo, log: ScrubLog) -> int:
    pf = pq.ParquetFile(src)
    src_types = {f.name: (f.type.value_type if pa.types.is_dictionary(f.type) else f.type)
                 for f in pf.schema_arrow}
    writer = None
    schema = None
    rows = 0
    for batch in pf.iter_batches(batch_size=100_000):
        df = batch.to_pandas()
        for c in df.columns:
            if isinstance(df[c].dtype, pd.CategoricalDtype):
                df[c] = df[c].astype(object)
        df[C_FN] = df[C_FN].fillna("").astype(str)
        df = transform(df, info, log)
        df["Site Key"] = [f or ("doc:" + d) for f, d in zip(df["Facility ID"], df["Document ID"])]
        df["key_county"] = df["Facility County"].map(norm_county)
        if schema is None:
            # types from the source file, not from this batch: a column that is
            # all-null in the first batch would otherwise be typed null
            back = {v: k for k, v in RENAME.items() if k in src_types}
            schema = pa.schema([pa.field(c, src_types.get(back.get(c, c), pa.string())) for c in df.columns])
            writer = pq.ParquetWriter(dst, schema, compression="zstd", use_dictionary=True)
        writer.write_table(pa.Table.from_pandas(df[schema.names], schema=schema, preserve_index=False))
        rows += len(df)
        print(f"  dashboard: {rows:,} rows", flush=True)
    writer.close()
    names = pq.ParquetFile(dst).schema_arrow.names
    leaked = sorted(set(names) & DROP)
    assert not leaked, f"identifying columns survived: {leaked}"
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--union", type=Path, default=ROOT / "data/processed/permit_data_union_v5g.csv")
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--label", default="v5g-deid")
    ap.add_argument("--dashboard-in", type=Path)
    ap.add_argument("--dashboard-out", type=Path)
    args = ap.parse_args()
    if not (args.out_dir or args.dashboard_out):
        ap.error("pass --out-dir and/or --dashboard-in/--dashboard-out")

    info = DocInfo(args.union, load_key())
    info.write_private_maps()
    if args.out_dir:
        log = ScrubLog()
        m = build_release(args.union, args.out_dir, info, log, args.label)
        print(json.dumps(m["tables"], indent=2))
        print(f"free-text cells changed: {sum(log.cells.values()):,}")
        for (c, lab), n in log.cells.most_common(20):
            print(f"  {c:45s} {lab:10s} {n:,}")
        print(f"residual check: {log.residual_hits} of {log.residual_checked:,} sampled rows still "
              "contain their full facility name in a scrubbed column")
        pd.DataFrame(log.samples).to_csv(PRIVATE / "deid_scrub_samples.csv", index=False)
        pd.DataFrame([{"label": k[0], "matched": k[1], "cells": n} for k, n in log.matched.most_common()]) \
            .to_csv(PRIVATE / "deid_matched_phrases.csv", index=False)
        print("most-replaced phrases:")
        for (lab, m), n in log.matched.most_common(25):
            print(f"  {n:6,}  {lab:10s} {m}")
        pd.DataFrame(log.residual_examples).to_csv(PRIVATE / "deid_residual_examples.csv", index=False)
    if args.dashboard_out:
        dlog = ScrubLog()
        n = build_dashboard(args.dashboard_in, args.dashboard_out, info, dlog)
        print(f"dashboard parquet: {n:,} rows -> {args.dashboard_out}; "
              f"free-text cells changed {sum(dlog.cells.values()):,}")


if __name__ == "__main__":
    main()
