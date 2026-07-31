"""Fourth-model judge over a multi-model validation run.

The validation harness (`validate_permit`) flags fields as disagreeing using
EXACT-MATCH-after-normalization (trim/lowercase/whitespace/numeric). That counts
"Title V" vs "Title V Operating Permit", or "baghouse" vs "fabric filter
(baghouse)", as disagreements even though they describe the same content.

This script adds a CONTENT-level agreement layer: a fourth, independent model
(default amazon/gpt-oss-120b) reads the three extractor outputs for each permit
PLUS the (truncated) source permit text, and for each key general field rules
whether the models agree on content and — on genuine conflicts — which value the
source supports. It then reports exact-match agreement vs content agreement
side by side, so we can see how much apparent disagreement is just formatting.

The judge reads only the compact JSON outputs + source text, so it is decoupled
from extraction and can be re-run/iterated without re-extracting.

Usage:
    PYTHONPATH=. python scripts/judge_validation_agreement.py [run.jsonl] [judge_model]
Defaults: latest validation_run_*.jsonl in the validation dir; judge amazon/gpt-oss-120b.
"""

import json
import os
import re
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path

import openai
from dotenv import dotenv_values


def _call_with_hard_timeout(fn, hard_seconds):
    """Run fn() on a daemon thread, abandon it if it exceeds hard_seconds.

    Mirrors the extractor watchdog: the SDK per-call timeout is not reliably
    honored on a wedged socket, so we bound wall-clock time and leave any stuck
    call on a daemon thread (which won't block process exit)."""
    box = {}

    def _run():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(hard_seconds)
    if t.is_alive():
        raise TimeoutError(f"judge call hard-timed out after {hard_seconds}s")
    if "e" in box:
        raise box["e"]
    return box["r"]

ROOT = Path(__file__).resolve().parents[1]
VAL_DIR = ROOT / "data" / "processed" / "validation"
TEXT_DIR = ROOT / "data" / "interim" / "extracted_text"

KEY_GENERAL_FIELDS = [
    "Facility Name", "Facility Address", "Facility City",
    "Facility State Abbreviation", "Facility Zip Code", "Permit Number",
    "Permit Type", "Issuance Date", "Expiration Date", "Regulatory Authority",
    "Primary Applicable Regulations (e.g., Title V, PSD, NESHAP Subpart)",
]

JUDGE_MODEL = sys.argv[2] if len(sys.argv) > 2 else "amazon/gpt-oss-120b"
SOURCE_CHAR_CAP = 200_000  # ~50k tokens; keeps judge well under its 131k-token ceiling

# Verdict buckets. "agree on content" = full | content | superset.
AGREE_BUCKETS = {"full", "content", "superset"}

SYSTEM = (
    "You are a meticulous adjudicator comparing structured data that three "
    "different AI models independently extracted from the SAME source permit "
    "document. For each field you decide whether the models AGREE ON CONTENT, "
    "not whether their strings match exactly. Same meaning in different wording, "
    "formatting, abbreviation, or units counts as agreement. You also use the "
    "source text to decide which value is correct when they genuinely conflict. "
    "Respond with ONLY a JSON object, no prose, no code fences."
)

PROMPT_TEMPLATE = """Three models (A, B, C) extracted these values for one permit. For EACH field below, classify the agreement and, when they conflict, say which value the SOURCE supports.

Per-field verdict must be one of:
- "full"        : all present values are effectively identical
- "content"     : present values mean the same thing (differ only in wording/format/abbrev/units)
- "superset"    : values are compatible; one is just more complete/specific (no contradiction)
- "conflict"    : at least two present values genuinely contradict each other
- "insufficient": fewer than two models gave a non-empty value

For "conflict", set "correct" to the letter(s) (e.g. "A", "A,C", or "none"/"unclear") of the model(s) whose value the SOURCE supports, and "correct_value" to the value the source supports. Otherwise leave "correct" as "" and "correct_value" as "".

Return JSON exactly shaped as:
{{"fields": {{"<field name>": {{"verdict": "...", "correct": "", "correct_value": "", "note": "<=12 words"}}}}, "units_overall": {{"verdict": "full|content|superset|conflict|insufficient", "note": "<=15 words on whether the emission-unit lists agree on content"}}, "overall_content_agreement": true|false}}

FIELD VALUES (model: value):
{field_block}

EMISSION UNITS (compact, per model):
{units_block}

SOURCE PERMIT TEXT (may be truncated):
\"\"\"
{source}
\"\"\"
"""


def _val(d, k):
    v = (d or {}).get(k)
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("", "nan", "none", "null", "n/a", "not provided", "information not provided") else s


def _client():
    # JUDGE_BASE_URL/JUDGE_API_KEY override the CBORG default, e.g. to run the
    # same judge model on Vertex MaaS (openai/gpt-oss-120b-maas) when CBORG's
    # budget is exhausted.
    base_url = os.getenv("JUDGE_BASE_URL", "https://api.cborg.lbl.gov")
    key = os.getenv("JUDGE_API_KEY") or dotenv_values().get("CBORG_API_KEY")
    return openai.OpenAI(api_key=key, base_url=base_url)


def _units_block(labeled_results):
    """Compact per-model unit list: ID | Description | Pollutants | Control | Fuel."""
    lines = []
    for letter, model, res in labeled_results:
        units = (res or {}).get("Emission Units") or []
        lines.append(f"Model {letter} ({model}): {len(units) if isinstance(units, list) else 0} units")
        if isinstance(units, list):
            for u in units[:25]:
                if not isinstance(u, dict):
                    continue
                bits = [_val(u, "Unit ID") or "?",
                        (_val(u, "Unit Description") or _val(u, "Unit Type"))[:50],
                        _val(u, "Pollutants")[:40],
                        _val(u, "Control Device(s)")[:40]]
                lines.append("   - " + " | ".join(b for b in bits if b))
    return "\n".join(lines) if lines else "(no units)"


def _parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def judge_record(client, rec):
    labeled = []
    for i, m in enumerate(rec.get("models", [])):
        if isinstance(m.get("result"), dict):
            labeled.append((chr(ord("A") + len(labeled)), m["model"], m["result"]))
    if len(labeled) < 2:
        return {"filename": rec.get("filename"), "skipped": "fewer than 2 model outputs",
                "n_models": len(labeled)}

    field_lines = []
    for f in KEY_GENERAL_FIELDS:
        vals = [f"  {letter}: {_val(res, f) or '(empty)'}" for letter, _, res in labeled]
        field_lines.append(f"[{f}]\n" + "\n".join(vals))
    field_block = "\n".join(field_lines)
    units_block = _units_block(labeled)

    src_path = TEXT_DIR / f"{rec.get('filename')}.txt"
    source = ""
    truncated = False
    if src_path.exists():
        source = src_path.read_text(encoding="utf-8", errors="ignore")
        if len(source) > SOURCE_CHAR_CAP:
            source = source[:SOURCE_CHAR_CAP]
            truncated = True

    prompt = PROMPT_TEMPLATE.format(field_block=field_block, units_block=units_block, source=source)
    soft = int(os.getenv("LLM_TIMEOUT_SECONDS", "240"))
    try:
        resp = _call_with_hard_timeout(
            lambda: client.chat.completions.create(
                model=JUDGE_MODEL,
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                temperature=0,
                timeout=soft,
            ),
            soft + 30,
        )
        raw = resp.choices[0].message.content
    except Exception as e:
        return {"filename": rec.get("filename"), "error": str(e)[:300]}

    parsed = _parse_json(raw or "")
    return {
        "filename": rec.get("filename"),
        "model_letters": {letter: model for letter, model, _ in labeled},
        "source_truncated": truncated,
        "judge": parsed,
        "raw": None if parsed else (raw or "")[:1000],
    }


def main():
    if len(sys.argv) > 1 and sys.argv[1].endswith(".jsonl"):
        run_path = Path(sys.argv[1])
    else:
        runs = sorted(VAL_DIR.glob("validation_run_*.jsonl"))
        runs = [r for r in runs if r.stat().st_size > 0]
        run_path = runs[-1]
    print(f"Judging run: {run_path.name}  with judge={JUDGE_MODEL}")

    recs = [json.loads(l) for l in open(run_path, encoding="utf-8") if l.strip()]
    body = [r for r in recs if not r.get("_run_meta")]
    print(f"  {len(body)} permit records")

    client = _client()
    out_path = run_path.with_name(run_path.stem + "_judge.jsonl")
    results = []
    with open(out_path, "w", encoding="utf-8") as fout:
        for i, rec in enumerate(body, 1):
            jr = judge_record(client, rec)
            results.append((rec, jr))
            fout.write(json.dumps(jr) + "\n")
            fout.flush()
            tag = jr.get("skipped") or jr.get("error") or ("ok" if jr.get("judge") else "unparsed")
            print(f"  [{i}/{len(body)}] {rec.get('filename')[:45]:45s} {tag}")

    summarize(run_path, body, results)


def summarize(run_path, body, results):
    # Exact-match baseline from stored field_summary; content agreement from judge.
    exact = defaultdict(lambda: {"present": 0, "agree": 0})
    content = defaultdict(lambda: {"judged": 0, "agree": 0})
    verdict_counts = Counter()
    flips = defaultdict(int)          # field -> exact-disagree but content-agree
    correctness = Counter()           # which model the source supported on conflicts
    judged_permits = parse_fail = skipped = 0

    rec_by_fn = {r.get("filename"): r for r in body}
    for rec, jr in results:
        if jr.get("skipped"):
            skipped += 1
            continue
        j = jr.get("judge")
        if not j or not isinstance(j.get("fields"), dict):
            parse_fail += 1
            continue
        judged_permits += 1
        fs = rec.get("field_summary") or {}
        for f in KEY_GENERAL_FIELDS:
            v = j["fields"].get(f) or {}
            verdict = (v.get("verdict") or "").strip().lower()
            if not verdict:
                continue
            verdict_counts[verdict] += 1
            # exact-match baseline for this field on this permit
            s = fs.get(f) or {}
            present = bool(s.get("complete_models") and len(s["complete_models"]) >= 2)
            exact_agree = present and not s.get("needs_review")
            if verdict != "insufficient":
                content[f]["judged"] += 1
                if verdict in AGREE_BUCKETS:
                    content[f]["agree"] += 1
            if present:
                exact[f]["present"] += 1
                if exact_agree:
                    exact[f]["agree"] += 1
            # a "flip": exact match said disagree, judge says content-agree
            if present and not exact_agree and verdict in AGREE_BUCKETS:
                flips[f] += 1
            if verdict == "conflict":
                correctness[v.get("correct") or "unclear"] += 1

    lines = [
        f"# Content-Agreement Judge Summary — {run_path.name}",
        "",
        f"Judge model: **{JUDGE_MODEL}**.  Permits judged: **{judged_permits}** "
        f"(skipped <2 models: {skipped}; judge parse failures: {parse_fail}).",
        "",
        "Exact = exact-match-after-normalization agreement (current harness). "
        "Content = judge ruling that models agree on meaning (full/content/superset). "
        "A 'flip' is a field the exact metric marked as disagreement but the judge "
        "ruled is the same content.",
        "",
        "## Per general field: exact-match vs content agreement",
        "",
        "| Field | Exact agree (present) | Content agree (judged) | Flips (format-only) |",
        "|---|---|---|---|",
    ]
    tot_ex_a = tot_ex_p = tot_co_a = tot_co_j = tot_flip = 0
    for f in KEY_GENERAL_FIELDS:
        ex, co = exact[f], content[f]
        ex_rate = f"{ex['agree']}/{ex['present']} ({100*ex['agree']/ex['present']:.0f}%)" if ex["present"] else "—"
        co_rate = f"{co['agree']}/{co['judged']} ({100*co['agree']/co['judged']:.0f}%)" if co["judged"] else "—"
        lines.append(f"| {f[:48]} | {ex_rate} | {co_rate} | {flips[f]} |")
        tot_ex_a += ex["agree"]; tot_ex_p += ex["present"]
        tot_co_a += co["agree"]; tot_co_j += co["judged"]; tot_flip += flips[f]
    ex_all = f"{tot_ex_a}/{tot_ex_p} ({100*tot_ex_a/tot_ex_p:.0f}%)" if tot_ex_p else "—"
    co_all = f"{tot_co_a}/{tot_co_j} ({100*tot_co_a/tot_co_j:.0f}%)" if tot_co_j else "—"
    lines += [
        f"| **ALL FIELDS** | **{ex_all}** | **{co_all}** | **{tot_flip}** |",
        "",
        "## Judge verdict distribution (all field judgments)",
        "",
    ]
    for v, c in verdict_counts.most_common():
        lines.append(f"- {v}: {c}")
    lines += ["", "## Source-adjudicated correctness on genuine conflicts", ""]
    if correctness:
        for who, c in correctness.most_common():
            lines.append(f"- source supported `{who}`: {c}")
    else:
        lines.append("- (no genuine conflicts judged)")

    out_md = run_path.with_name(run_path.stem + "_judge_summary.md")
    out_md.write_text("\n".join(lines) + "\n")
    out_json = run_path.with_name(run_path.stem + "_judge_summary.json")
    out_json.write_text(json.dumps({
        "run": run_path.name, "judge_model": JUDGE_MODEL,
        "judged_permits": judged_permits, "skipped": skipped, "parse_failures": parse_fail,
        "exact": {f: dict(exact[f]) for f in KEY_GENERAL_FIELDS},
        "content": {f: dict(content[f]) for f in KEY_GENERAL_FIELDS},
        "flips": dict(flips), "verdicts": dict(verdict_counts),
        "conflict_correctness": dict(correctness),
        "overall": {"exact": [tot_ex_a, tot_ex_p], "content": [tot_co_a, tot_co_j], "flips": tot_flip},
    }, indent=2))
    print("\n".join(lines))
    print(f"\n[write] {out_md}\n[write] {out_json}\n[write] {run_path.with_name(run_path.stem + '_judge.jsonl')}")


if __name__ == "__main__":
    main()
