"""Stage 2 of NAICS backfill: classify the remaining permits with an LLM.

Takes the work-list left by backfill_naics.py (permits with no permit-stated,
ECHO-matched, derived, or SIC-crosswalked code) and asks a model to assign a
6-digit NAICS code from the facility name, industry description, and a sample
unit description. Runs as a Vertex batch job -- the prompts are small, so the
whole work-list costs a couple of dollars.

The model is instructed to return an empty code rather than guess when the
text is uninformative, and to avoid the 339999 catch-all that made the
pipeline's existing derived field unreliable. Confidence is returned alongside
so low-confidence assignments can be filtered.

    python scripts/classify_naics_llm.py build   [--limit N]
    python scripts/classify_naics_llm.py submit
    python scripts/classify_naics_llm.py merge          # -> naics_llm_results.csv
"""
import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
csv.field_size_limit(sys.maxsize)

ANALYSIS = Path("data/processed/analysis")
WORKLIST = ANALYSIS / "naics_llm_worklist.csv"
BATCH_DIR = Path("data/processed/gcp_batch")
INPUTS = BATCH_DIR / "naics_classify_inputs.jsonl"
RESULTS = ANALYSIS / "naics_llm_results.csv"
BUCKET = "permit-data-extraction-corpus"
PROJECT = "permit-data-extraction"
LOCATION = "us-central1"
MARKER_RX = re.compile(r"\[\[FILE:(.+?)\]\]")

# propertyOrdering puts the answer first, so a truncated response still
# carries the code. maxOutputTokens must leave room for THINKING tokens:
# gemini-2.5-flash spends them against this budget, and 512 truncated 35% of
# responses mid-JSON ('{"basis": "').
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "naics_code": {"type": "STRING", "nullable": True},
        "confidence": {"type": "STRING", "nullable": True},
        "basis": {"type": "STRING", "nullable": True},
    },
    "propertyOrdering": ["naics_code", "confidence", "basis"],
}
MAX_OUTPUT_TOKENS = 2048

PROMPT = """You assign North American Industry Classification System (NAICS) codes to industrial facilities that hold air quality permits.

Given the facility information below, return the single most specific 6-digit NAICS code describing the facility's PRIMARY activity.

Rules:
- Return a real 6-digit NAICS code. The first two digits must be a valid NAICS sector.
- Classify the FACILITY's primary business, not the individual equipment. A boiler at a paper mill means the facility is a paper mill (322xxx), not a power plant.
- Do NOT use 339999 ("All Other Miscellaneous Manufacturing") or other "all other" catch-all codes unless the facility genuinely fits no more specific code.
- If the information is too vague to classify (e.g. only a company name with no industry signal), return an empty naics_code rather than guessing.
- confidence: "high" if the text clearly identifies the industry, "medium" if inferred from partial signals, "low" if a guess.
- basis: at most 12 words naming the evidence you used.

Facility name: {facility}
State: {state}
Industry description: {industry}
Permit type: {permit_type}
Example permitted unit: {unit}
"""


def rows():
    return list(csv.DictReader(WORKLIST.open(encoding="utf-8", errors="replace")))


def cmd_build(args):
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    items = rows()
    if args.only_missing and RESULTS.exists():
        done = {r["filename"] for r in csv.DictReader(RESULTS.open(encoding="utf-8"))
                if r["naics_llm"]}
        items = [r for r in items if r["filename"] not in done]
        print(f"resuming: {len(items):,} permits still without a code")
    if args.limit:
        items = items[: args.limit]
    n = 0
    with INPUTS.open("w", encoding="utf-8") as f:
        for r in items:
            if not (r["facility"] or r["industry_description"]):
                continue
            text = f"[[FILE:{r['filename']}]]\n" + PROMPT.format(
                facility=r["facility"] or "(not stated)",
                state=r["state"] or "(not stated)",
                industry=r["industry_description"] or "(not stated)",
                permit_type=r["permit_type"] or "(not stated)",
                unit=r["unit_description"] or "(not stated)")
            f.write(json.dumps({"request": {
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": MAX_OUTPUT_TOKENS,
                                     "responseMimeType": "application/json",
                                     "responseSchema": SCHEMA},
            }}) + "\n")
            n += 1
    print(f"{INPUTS}: {n:,} classification requests "
          f"({INPUTS.stat().st_size/1e6:.1f} MB)")


def cmd_submit(args):
    from google import genai
    from google.genai.types import CreateBatchJobConfig
    gcs_in = f"gs://{BUCKET}/batch_inputs/{INPUTS.name}"
    gcs_out = f"gs://{BUCKET}/batch_outputs/{INPUTS.stem}/"
    subprocess.run(["gsutil", "-q", "cp", str(INPUTS), gcs_in], check=True)
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    job = client.batches.create(model=args.model, src=gcs_in,
                                config=CreateBatchJobConfig(dest=gcs_out))
    print(f"submitted: {job.name}\n  state: {job.state}\n  out: {gcs_out}")


def cmd_merge(args):
    src = args.src or f"gs://{BUCKET}/batch_outputs/{INPUTS.stem}/"
    out, bad = [], 0
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["gsutil", "-m", "-q", "cp", "-r", src.rstrip("/") + "/*", td],
                       check=True)
        for p in sorted(Path(td).rglob("*.jsonl")):
            with p.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    m = MARKER_RX.search(
                        rec["request"]["contents"][0]["parts"][0]["text"])
                    if not m:
                        bad += 1
                        continue
                    try:
                        d = json.loads(
                            rec["response"]["candidates"][0]["content"]["parts"][0]["text"])
                    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                        bad += 1
                        continue
                    code = re.sub(r"\D", "", str(d.get("naics_code") or ""))
                    out.append({"filename": m.group(1),
                                "naics_llm": code if len(code) == 6 else "",
                                "confidence": (d.get("confidence") or "").lower(),
                                "basis": (d.get("basis") or "")[:80]})
    prior = {}
    if args.append and RESULTS.exists():
        for r in csv.DictReader(RESULTS.open(encoding="utf-8")):
            if r["naics_llm"]:
                prior[r["filename"]] = r
    for r in out:
        if r["naics_llm"] or r["filename"] not in prior:
            prior[r["filename"]] = r
    with RESULTS.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "naics_llm", "confidence", "basis"])
        w.writeheader()
        w.writerows(prior.values())
    out = list(prior.values())
    got = sum(1 for r in out if r["naics_llm"])
    from collections import Counter
    conf = Counter(r["confidence"] for r in out if r["naics_llm"])
    print(f"{RESULTS}: {len(out):,} responses, {got:,} with a code "
          f"({got/max(len(out),1):.0%}), {bad} unparsed")
    print("  confidence:", dict(conf.most_common()))
    junk = sum(1 for r in out if r["naics_llm"] == "339999")
    print(f"  339999 assignments: {junk} (should be near zero)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--limit", type=int, default=0)
    b.add_argument("--only-missing", action="store_true",
                   help="skip permits that already have a code in the results CSV")
    b.set_defaults(fn=cmd_build)
    s = sub.add_parser("submit")
    s.add_argument("--model", default="gemini-2.5-flash")
    s.set_defaults(fn=cmd_submit)
    m = sub.add_parser("merge")
    m.add_argument("--src")
    m.add_argument("--append", action="store_true",
                   help="merge into existing results instead of replacing")
    m.set_defaults(fn=cmd_merge)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
