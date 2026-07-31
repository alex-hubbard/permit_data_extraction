"""Build (and optionally submit) Vertex AI Gemini batch-prediction inputs.

Turns a paid-queue lane CSV (data/processed/paid_queue/*.csv) into batch
JSONL: one request per 80K-char chunk, using the pipeline's PROMPT_TEMPLATE
and a response_schema built from GENERAL_TARGET_FIELDS/UNIT_DETAIL_FIELDS
(structured output — no JSON-decode failures).

Each request's prompt starts with a "[[DOC:<stem>::chunk<i>/<n>]]" marker
line. Vertex batch output echoes the request alongside each response, so the
merger recovers (stem, chunk) from the marker regardless of output order.

Usage:
    python scripts/build_vertex_batch_inputs.py build LANE.csv [LANE2.csv ...] \
        [--out data/processed/gcp_batch/<lane>.jsonl] [--model gemini-2.5-flash] \
        [--chunk-chars 80000] [--limit N]
    python scripts/build_vertex_batch_inputs.py submit INPUTS.jsonl \
        [--bucket permit-data-extraction-corpus] [--model gemini-2.5-flash]
    python scripts/build_vertex_batch_inputs.py status [JOB_NAME]

Lane CSV shapes handled: any CSV with a `filename` column (TX/WI/SCAQMD/MN/
retry lanes; .pdf extension stripped) or the LA lane (ai/doc_id/description →
stem reconstructed as in build_paid_priority_lists.py).
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from permit_data_extraction.dataset import (  # noqa: E402
    GENERAL_TARGET_FIELDS,
    PROMPT_TEMPLATE,
    UNIT_DETAIL_FIELDS,
    _split_text_into_chunks,
)

TEXT_DIR = Path("data/interim/extracted_text")
OUT_DIR = Path("data/processed/gcp_batch")
PROJECT = "permit-data-extraction"
LOCATION = "us-central1"
DOC_MARKER = "[[DOC:{stem}::chunk{i}/{n}]]"
DOC_MARKER_RX = re.compile(r"\[\[DOC:(.+?)::chunk(\d+)/(\d+)\]\]")


def sanitize_field(name: str) -> str:
    """BigQuery-safe field name: Vertex batch ingests request JSONL via
    BigQuery, which rejects '/', '(', ')' etc. in responseSchema keys."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")


# sanitized -> original pipeline field name, for the output merger
FIELD_UNSANITIZE = {sanitize_field(f): f
                    for f in [*GENERAL_TARGET_FIELDS, "Emission Units", *UNIT_DETAIL_FIELDS]}
assert len(FIELD_UNSANITIZE) == len(GENERAL_TARGET_FIELDS) + 1 + len(UNIT_DETAIL_FIELDS), \
    "field-name sanitization collision"


def response_schema() -> dict:
    """Nullable-string schema over the pipeline's target fields (sanitized names)."""
    s = {"type": "STRING", "nullable": True}
    return {
        "type": "OBJECT",
        "properties": {
            **{sanitize_field(f): s for f in GENERAL_TARGET_FIELDS},
            sanitize_field("Emission Units"): {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {sanitize_field(f): s for f in UNIT_DETAIL_FIELDS},
                },
            },
        },
    }


def lane_stems(lane_csv: Path):
    rows = list(csv.DictReader(lane_csv.open(encoding="utf-8", errors="ignore")))
    if not rows:
        return
    if "filename" in rows[0]:
        for r in rows:
            yield re.sub(r"\.pdf$", "", r["filename"], flags=re.I)
    elif "doc_id" in rows[0]:  # LA lane: reconstruct downloader stem
        for r in rows:
            ai = (r.get("ai") or "noai")
            date = (r.get("description_date") or "")[:10]
            # match build_paid_priority_lists.lane_la exactly
            desc = re.sub(r"[^A-Za-z0-9._-]+", "_", r.get("description") or "")[:60].strip("_")
            # date lives in the la index; lane CSV lacks it, so glob instead
            hits = list(TEXT_DIR.glob(f"AI{ai}_{r['doc_id']}_*.txt"))
            if hits:
                yield hits[0].stem
            else:
                yield f"AI{ai}_{r['doc_id']}_{date}_{desc}".strip("_")
    else:
        raise SystemExit(f"{lane_csv}: unrecognized lane CSV columns {list(rows[0])}")


def cmd_build(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    schema = response_schema()
    out = Path(args.out) if args.out else OUT_DIR / (Path(args.lanes[0]).stem + ".jsonl")
    n_docs = n_chunks = n_missing = 0
    seen = set()
    with out.open("w", encoding="utf-8") as f:
        for lane in args.lanes:
            for stem in lane_stems(Path(lane)):
                if stem in seen:
                    continue
                seen.add(stem)
                txt = TEXT_DIR / (stem + ".txt")
                if not txt.exists():
                    n_missing += 1
                    continue
                text = txt.read_text(encoding="utf-8", errors="ignore")
                chunks = _split_text_into_chunks(text, args.chunk_chars)
                n_docs += 1
                for i, chunk in enumerate(chunks, 1):
                    marker = DOC_MARKER.format(stem=stem, i=i, n=len(chunks))
                    prompt = marker + "\n" + PROMPT_TEMPLATE.replace("{permit_text}", chunk)
                    f.write(json.dumps({"request": {
                        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.1,
                            # 65535 not 65536: flash-lite's range is exclusive of 65536
                            "maxOutputTokens": 65535,
                            "responseMimeType": "application/json",
                            "responseSchema": schema,
                        },
                    }}) + "\n")
                    n_chunks += 1
                if args.limit and n_docs >= args.limit:
                    break
    mb = out.stat().st_size / 1e6
    print(f"{out}: {n_docs} docs -> {n_chunks} requests ({mb:.0f}MB); "
          f"{n_missing} skipped (no text file)")


def cmd_submit(args):
    from google import genai
    from google.genai.types import CreateBatchJobConfig
    src_local = Path(args.inputs)
    gcs_in = f"gs://{args.bucket}/batch_inputs/{src_local.name}"
    gcs_out = f"gs://{args.bucket}/batch_outputs/{src_local.stem}/"
    import subprocess
    subprocess.run(["gsutil", "-q", "cp", str(src_local), gcs_in], check=True)
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    job = client.batches.create(model=args.model, src=gcs_in,
                                config=CreateBatchJobConfig(dest=gcs_out))
    print(f"submitted: {job.name}\n  state: {job.state}\n  in:  {gcs_in}\n  out: {gcs_out}")


def cmd_status(args):
    from google import genai
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    if args.job:
        job = client.batches.get(name=args.job)
        print(job.name, job.state)
    else:
        for job in client.batches.list():
            print(job.name, job.state)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("lanes", nargs="+")
    b.add_argument("--out")
    b.add_argument("--model", default="gemini-2.5-flash")
    b.add_argument("--chunk-chars", type=int, default=80_000)
    b.add_argument("--limit", type=int, default=0)
    b.set_defaults(fn=cmd_build)
    s = sub.add_parser("submit")
    s.add_argument("inputs")
    s.add_argument("--bucket", default="permit-data-extraction-corpus")
    s.add_argument("--model", default="gemini-2.5-flash")
    s.set_defaults(fn=cmd_submit)
    st = sub.add_parser("status")
    st.add_argument("job", nargs="?")
    st.set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
