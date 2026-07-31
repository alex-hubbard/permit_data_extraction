"""Append no-text-recovery successes to the free-model re-extraction queue.

Reads notext_recovery.jsonl (from recover_no_text_pdfs.py), takes every file
with a recovered text file, and appends the ones not already listed to
freemodel_list.json (atomic write). The running run_freemodel_reextract.sh
wrapper re-reads the list on each loop attempt, so these get picked up
automatically — no restart needed.
"""
import json
from pathlib import Path

MANIFEST = Path("data/processed/reextraction/notext_recovery.jsonl")
LIST = Path("data/processed/reextraction/freemodel_list.json")
TEXT_DIR = Path("data/interim/extracted_text")

recovered = set()
for line in MANIFEST.read_text().splitlines():
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    if rec.get("status") == "ok":
        recovered.add(rec["filename"])

# only queue files whose text actually exists
recovered = {f for f in recovered if (TEXT_DIR / (f + ".txt")).exists()}

listed = json.loads(LIST.read_text())
new = sorted(recovered - set(listed))
print(f"recovered with text: {len(recovered)} | already listed: {len(recovered) - len(new)} | appending: {len(new)}")
if new:
    updated = listed + new
    tmp = LIST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(updated, indent=1))
    tmp.rename(LIST)
    print(f"list now {len(updated)} files")
