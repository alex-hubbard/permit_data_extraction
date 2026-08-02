"""Reassemble Vertex batch-prediction outputs into freemodel-style results.

Reads the predictions JSONL files a Gemini batch job writes under its GCS
output prefix (or a local copy), recovers (doc stem, chunk i/n) from the
[[DOC:...]] marker echoed with each request, un-sanitizes field names back to
the pipeline's originals, merges each document's chunks with
_merge_chunk_extractions, and appends records shaped exactly like
freemodel_results.jsonl — so batch_reextract.py --finalize and
merge_freemodel_into_union.py work on them unchanged.

Usage:
    python scripts/merge_vertex_batch_outputs.py gs://BUCKET/batch_outputs/PREFIX/ \
        --results-jsonl data/processed/gcp_batch/RESULTS.jsonl --model gemini-2.5-flash
"""
import argparse
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from permit_data_extraction.dataset import _merge_chunk_extractions  # noqa: E402
from build_vertex_batch_inputs import DOC_MARKER_RX, FIELD_UNSANITIZE  # noqa: E402


def unsanitize(obj):
    if isinstance(obj, dict):
        return {FIELD_UNSANITIZE.get(k, k): unsanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [unsanitize(x) for x in obj]
    return obj


def iter_prediction_lines(src: str):
    # Stream line-by-line: prediction files can be multiple GB, and
    # read_text() on those OOM-kills small machines silently.
    if src.startswith("gs://"):
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["gsutil", "-m", "-q", "cp", "-r", src.rstrip("/") + "/*", td],
                           check=True)
            for f in sorted(Path(td).rglob("*.jsonl")):
                with f.open(encoding="utf-8") as fh:
                    yield from fh
    else:
        for f in sorted(Path(src).rglob("*.jsonl")):
            with f.open(encoding="utf-8") as fh:
                yield from fh


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", help="GCS prefix or local dir of batch outputs")
    ap.add_argument("--results-jsonl", required=True)
    ap.add_argument("--model", default="gemini-2.5-flash")
    args = ap.parse_args()

    docs = defaultdict(dict)   # stem -> {chunk_i: parsed dict}
    totals = defaultdict(int)  # stem -> n chunks expected
    chars = defaultdict(int)   # stem -> doc chars (sum of chunk lens, marker-less)
    failed = defaultdict(int)  # stem -> failed chunks
    n_lines = n_unparsed = 0
    for line in iter_prediction_lines(args.src):
        if not line.strip():
            continue
        n_lines += 1
        rec = json.loads(line)
        req_text = rec["request"]["contents"][0]["parts"][0]["text"]
        m = DOC_MARKER_RX.search(req_text)
        if not m:
            n_unparsed += 1
            continue
        stem, i, n = m.group(1), int(m.group(2)), int(m.group(3))
        totals[stem] = n
        chars[stem] += len(req_text) - (m.end() - m.start())
        resp = rec.get("response")
        try:
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            docs[stem][i] = unsanitize(json.loads(text))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            failed[stem] += 1

    out = Path(args.results_jsonl)
    out.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now().isoformat(timespec="seconds")
    n_ok = n_failed_docs = 0
    with out.open("a", encoding="utf-8") as f:
        for stem in sorted(totals):
            chunks = [docs[stem][i] for i in sorted(docs[stem])]
            n_missing = totals[stem] - len(chunks) - failed[stem]
            if not chunks:
                f.write(json.dumps({"filename": stem, "error": "no chunk succeeded",
                                    "started": started}) + "\n")
                n_failed_docs += 1
                continue
            merged = _merge_chunk_extractions(chunks)
            f.write(json.dumps({
                "filename": stem,
                "started": started,
                "model": args.model,
                "doc_chars": chars[stem],
                "pieces_ok": len(chunks),
                "pieces_failed": failed[stem] + n_missing,
                "n_units": len(merged.get("Emission Units") or []),
                "extraction": merged,
            }) + "\n")
            n_ok += 1
    print(f"{out}: {n_ok} docs merged, {n_failed_docs} failed, "
          f"{n_lines} response lines ({n_unparsed} without marker)")


if __name__ == "__main__":
    main()
