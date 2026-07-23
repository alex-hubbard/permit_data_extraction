"""Build deduped priority extraction lists for the Aug 1 paid-credit runs.

One CSV per lane under data/processed/paid_queue/, deduped to the
latest/most-substantive document per facility (the coverage-critical
subset), plus a summary with document counts and token estimates:

  tx_sop.csv       TX Site Operating Permits, latest per permit number
  wi_final.csv     WI final permits already text-extracted
  la_latest.csv    LA: per AI, all sections of the highest permit revision
  scaqmd_latest.csv  SCAQMD: per facility, highest revision on disk
  mn_latest.csv    MN: whatever WIMN has downloaded (already 1/facility)
  retry_failed.csv freemodel docs with >=1 failed piece

Rerunnable: reads whatever is on disk now, so rerun right before the paid
runs to pick up scraper progress (LA OCR, SCAQMD/OH/MN still downloading).
OH is excluded until its crawl is far enough along to dedupe sensibly.

Token estimates: len(text)/4 where extracted text exists; for LA docs not
yet OCR'd, index pages * 2000 chars/page.
"""
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.config import PROCESSED_DATA_DIR, RAW_DATA_DIR

TEXT_DIR = Path("data/interim/extracted_text")
OUT_DIR = PROCESSED_DATA_DIR / "paid_queue"

LA_PERMIT_RX = re.compile(r"\b(\d{4}-\d{5})-V(\d+)\b", re.I)
LA_TITLEV_RX = re.compile(r"part\s*70|title\s*v\b|\b\d{4}-\d{5}-V\d+\b", re.I)


def text_tokens(stem: str):
    t = TEXT_DIR / (stem + ".txt")
    if t.exists():
        return t.stat().st_size // 4, True
    return None, False


def write_lane(name, rows, fields):
    path = OUT_DIR / name
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


def lane_tx():
    log = RAW_DATA_DIR / "tx_tceq_titlev_permits" / "tx_cfr_download_log.csv"
    latest = {}  # permit_number -> row with max date
    with open(log, encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "downloaded" or r.get("doc_title") != "Site Operating Permit":
                continue
            m = re.match(r"permit-[^_]+_\d+_(\d+)-(\d+)-(\d{4})_", r["filename"])
            key = (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)
            prev = latest.get(r["permit_number"])
            if prev is None or key > prev[0]:
                latest[r["permit_number"]] = (key, r)
    rows = []
    for _, r in latest.values():
        toks, have = text_tokens(Path(r["filename"]).stem)
        rows.append({"permit_number": r["permit_number"], "filename": r["filename"],
                     "est_tokens": toks or "", "text_ready": have})
    return rows, ["permit_number", "filename", "est_tokens", "text_ready"]


def lane_wi():
    pdf_dir = RAW_DATA_DIR / "wi_dnr_titlev_permits" / "pdfs"
    rows = []
    for p in sorted(pdf_dir.glob("*.pdf")):
        toks, have = text_tokens(p.stem)
        rows.append({"filename": p.name, "est_tokens": toks or "", "text_ready": have})
    return rows, ["filename", "est_tokens", "text_ready"]


def lane_la():
    idx = RAW_DATA_DIR / "la_edms_titlev_permits" / "la_edms_air_permits_index.csv"
    by_ai = defaultdict(list)
    with open(idx, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["document_subtype"] != "Final Permit" or not LA_TITLEV_RX.search(r["description"] or ""):
                continue
            ai = (r["ai_numbers"] or "na").split(";")[0]
            m = LA_PERMIT_RX.search(r["description"] or "")
            by_ai[ai].append((m.group(1) if m else None, int(m.group(2)) if m else -1, r))
    rows = []
    for ai, docs in by_ai.items():
        # per permit base, keep only the highest V revision (all its sections);
        # unparseable descriptions fall back to latest document_date per AI
        best_v = {}
        for base, v, r in docs:
            if base is not None:
                best_v[base] = max(best_v.get(base, -1), v)
        keep = [r for base, v, r in docs if base is not None and v == best_v[base]]
        unparsed = [r for base, _, r in docs if base is None]
        if not keep and unparsed:
            keep = [max(unparsed, key=lambda r: r["document_date"] or "")]
        for r in keep:
            stem = None  # filename convention from the downloader
            ai_n = (r["ai_numbers"] or "").split(";")[0] or "noai"
            date = (r["document_date"] or "")[:10]
            desc = re.sub(r"[^A-Za-z0-9._-]+", "_", r["description"] or "")[:60].strip("_")
            stem = f"AI{ai_n}_{r['doc_id']}_{date}_{desc}".strip("_")
            toks, have = text_tokens(stem)
            if toks is None:
                try:
                    toks = int(r["pages"] or 0) * 2000 // 4
                except ValueError:
                    toks = ""
            rows.append({"ai": ai, "doc_id": r["doc_id"], "description": r["description"],
                         "pages": r["pages"], "est_tokens": toks, "text_ready": have})
    return rows, ["ai", "doc_id", "description", "pages", "est_tokens", "text_ready"]


def lane_scaqmd():
    pdf_dir = RAW_DATA_DIR / "aqmd_titlev_permits" / "pdfs"
    best = {}
    for p in pdf_dir.glob("*.pdf"):
        m = re.match(r"facID-(\d+)_rev-(\d+)", p.name)
        if not m:
            continue
        fac, rev = m.group(1), int(m.group(2))
        if fac not in best or rev > best[fac][0]:
            best[fac] = (rev, p)
    rows = []
    for fac, (rev, p) in sorted(best.items()):
        toks, have = text_tokens(p.stem)
        if toks is None:
            # text layer confirmed present; ~550 chars/KB of PDF observed
            toks = ""
        rows.append({"facility_id": fac, "revision": rev, "filename": p.name,
                     "est_tokens": toks, "text_ready": have})
    return rows, ["facility_id", "revision", "filename", "est_tokens", "text_ready"]


def lane_mn():
    pdf_dir = RAW_DATA_DIR / "mn_pca_air_latest_permits"
    rows = []
    for p in sorted(pdf_dir.glob("*.pdf")):
        toks, have = text_tokens(p.stem)
        rows.append({"filename": p.name, "est_tokens": toks or "", "text_ready": have})
    return rows, ["filename", "est_tokens", "text_ready"]


def lane_retry():
    results = PROCESSED_DATA_DIR / "reextraction" / "freemodel_results.jsonl"
    rows = []
    seen = set()
    for line in results.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("pieces_failed", 0) > 0 and r["filename"] not in seen:
            seen.add(r["filename"])
            rows.append({"filename": r["filename"],
                         "pieces_ok": r.get("pieces_ok", ""),
                         "pieces_failed": r["pieces_failed"],
                         "doc_chars": r.get("doc_chars", "")})
    return rows, ["filename", "pieces_ok", "pieces_failed", "doc_chars"]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lanes = {
        "tx_sop.csv": lane_tx,
        "wi_final.csv": lane_wi,
        "la_latest.csv": lane_la,
        "scaqmd_latest.csv": lane_scaqmd,
        "mn_latest.csv": lane_mn,
        "retry_failed.csv": lane_retry,
    }
    summary = []
    for name, fn in lanes.items():
        rows, fields = fn()
        write_lane(name, rows, fields)
        toks = sum(int(r.get("est_tokens") or 0) for r in rows)
        ready = sum(1 for r in rows if r.get("text_ready") is True)
        summary.append((name, len(rows), toks, ready))
        print(f"{name:<20} {len(rows):>6} docs  {toks/1e6:>7.1f}M est tokens  "
              f"({ready} text-ready)")
    total_docs = sum(s[1] for s in summary)
    total_toks = sum(s[2] for s in summary)
    print(f"{'TOTAL':<20} {total_docs:>6} docs  {total_toks/1e6:>7.1f}M est tokens")


if __name__ == "__main__":
    main()
