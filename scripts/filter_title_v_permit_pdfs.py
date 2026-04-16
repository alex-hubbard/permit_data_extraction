#!/usr/bin/env python3
"""
Lightweight PDF filter to keep only Title V permit documents.

By default, this script scans PDFs in a directory, keeps likely Title V permits,
and moves everything else to a quarantine subfolder.
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PyPDF2 import PdfReader


KEEP_DECISION = "keep_title_v_permit"
DROP_DECISION = "not_title_v_permit"
UNKNOWN_DECISION = "unreadable_or_uncertain"


@dataclass
class Decision:
    decision: str
    reason: str
    title_v_hits: int
    permit_hits: int
    negative_hits: int


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def count_matches(text: str, patterns: Iterable[str]) -> int:
    total = 0
    for pat in patterns:
        total += len(re.findall(pat, text))
    return total


def classify_text(text: str) -> Decision:
    t = normalize_text(text)
    if not t:
        return Decision(UNKNOWN_DECISION, "no_text_extracted", 0, 0, 0)

    title_v_patterns = [
        r"\btitle\s*v\b",
        r"\btitle\s*5\b",
        r"\bpart\s*70\b",
        r"\b70\.\d+\b",
    ]
    permit_patterns = [
        r"\bpermit\b",
        r"\boperating permit\b",
        r"\btitle v operating permit\b",
        r"\bpermit number\b",
    ]
    negative_patterns = [
        r"\bpublic notice\b",
        r"\bnotice of\b",
        r"\bhearing\b",
        r"\bfact sheet\b",
        r"\bapplication\b",
        r"\brequest for\b",
        r"\bdraft\b",
        r"\bcomment period\b",
    ]

    title_v_hits = count_matches(t, title_v_patterns)
    permit_hits = count_matches(t, permit_patterns)
    negative_hits = count_matches(t, negative_patterns)

    # Strong positive: explicit Title V + permit language.
    if title_v_hits >= 1 and permit_hits >= 2:
        return Decision(
            KEEP_DECISION,
            "title_v_and_permit_language",
            title_v_hits,
            permit_hits,
            negative_hits,
        )

    # Slightly weaker but still acceptable.
    if title_v_hits >= 2 and permit_hits >= 1:
        return Decision(
            KEEP_DECISION,
            "multiple_title_v_signals",
            title_v_hits,
            permit_hits,
            negative_hits,
        )

    # Drop: no real Title V/permit evidence.
    if title_v_hits == 0 or permit_hits == 0:
        return Decision(
            DROP_DECISION,
            "missing_title_v_or_permit_signals",
            title_v_hits,
            permit_hits,
            negative_hits,
        )

    # Ambiguous middle: keep out of main corpus unless user reviews.
    return Decision(
        UNKNOWN_DECISION,
        "ambiguous_signals",
        title_v_hits,
        permit_hits,
        negative_hits,
    )


def extract_pdf_text(pdf_path: Path, max_pages: int) -> str:
    pieces: list[str] = []
    with pdf_path.open("rb") as f:
        reader = PdfReader(f)
        for page in reader.pages[:max_pages]:
            pieces.append(page.extract_text() or "")
    return "\n".join(pieces)


def ensure_unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    i = 1
    while True:
        candidate = dest.with_name(f"{stem}__dup{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter PDFs to keep only likely Title V permits.")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/raw/idem_title_v"),
        help="Directory containing downloaded PDFs.",
    )
    parser.add_argument(
        "--quarantine-dir",
        type=Path,
        default=None,
        help="Where to move non-matching files (default: <input-dir>/_non_title_v).",
    )
    parser.add_argument(
        "--action",
        choices=["move", "delete", "report"],
        default="move",
        help="How to handle non-matches and ambiguous files.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=8,
        help="Max pages to scan per PDF for speed.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Optional cap for testing (0 = all files).",
    )
    parser.add_argument(
        "--report-csv",
        type=Path,
        default=None,
        help="Path for CSV report (default: <input-dir>/title_v_filter_report.csv).",
    )
    args = parser.parse_args()

    input_dir = args.input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Input directory does not exist: {input_dir}")

    quarantine_dir = (
        args.quarantine_dir.expanduser().resolve()
        if args.quarantine_dir
        else (input_dir / "_non_title_v")
    )
    report_csv = (
        args.report_csv.expanduser().resolve()
        if args.report_csv
        else (input_dir / "title_v_filter_report.csv")
    )

    pdfs = sorted(input_dir.glob("*.pdf"))
    if args.max_files > 0:
        pdfs = pdfs[: args.max_files]

    if args.action == "move":
        quarantine_dir.mkdir(parents=True, exist_ok=True)

    kept = 0
    moved = 0
    deleted = 0
    uncertain = 0
    failed = 0

    report_csv.parent.mkdir(parents=True, exist_ok=True)
    with report_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "file",
                "decision",
                "reason",
                "title_v_hits",
                "permit_hits",
                "negative_hits",
                "action_taken",
                "error",
            ],
        )
        writer.writeheader()

        for pdf_path in pdfs:
            action_taken = "none"
            error_msg = ""
            try:
                text = extract_pdf_text(pdf_path, max_pages=args.max_pages)
                decision = classify_text(text)

                if decision.decision == KEEP_DECISION:
                    kept += 1
                else:
                    if decision.decision == UNKNOWN_DECISION:
                        uncertain += 1

                    if args.action == "move":
                        dest = ensure_unique_dest(quarantine_dir / pdf_path.name)
                        pdf_path.rename(dest)
                        action_taken = f"moved->{dest.name}"
                        moved += 1
                    elif args.action == "delete":
                        pdf_path.unlink(missing_ok=False)
                        action_taken = "deleted"
                        deleted += 1
                    else:
                        action_taken = "report_only"

                writer.writerow(
                    {
                        "file": pdf_path.name,
                        "decision": decision.decision,
                        "reason": decision.reason,
                        "title_v_hits": decision.title_v_hits,
                        "permit_hits": decision.permit_hits,
                        "negative_hits": decision.negative_hits,
                        "action_taken": action_taken,
                        "error": error_msg,
                    }
                )
            except Exception as exc:
                failed += 1
                writer.writerow(
                    {
                        "file": pdf_path.name,
                        "decision": UNKNOWN_DECISION,
                        "reason": "processing_error",
                        "title_v_hits": 0,
                        "permit_hits": 0,
                        "negative_hits": 0,
                        "action_taken": "none",
                        "error": str(exc),
                    }
                )

    print(f"Scanned: {len(pdfs)}")
    print(f"Kept: {kept}")
    print(f"Moved: {moved}")
    print(f"Deleted: {deleted}")
    print(f"Uncertain: {uncertain}")
    print(f"Failed: {failed}")
    print(f"Report: {report_csv}")
    if args.action == "move":
        print(f"Quarantine dir: {quarantine_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
