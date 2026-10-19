"""Show each eval question with the paragraph around its evidence, for fast review.

    uv run --group ingest python scripts/review_questions.py                 # pending only
    uv run --group ingest python scripts/review_questions.py --ids q03,u05
    uv run --group ingest python scripts/review_questions.py --interactive

--interactive asks for a verdict per question and writes it into that question's review
field, leaving every other byte of questions.yaml unchanged. After changing a question's
text yourself, set its review to "edited" by hand.
"""

import argparse
import re
import sys
import textwrap
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from ingest.clean import clean_document
from ingest.extract import extract_pages

ROOT = Path(__file__).resolve().parents[1]
VERDICTS = {"a": "accepted", "d": "dropped"}
WRAP = 100


@cache
def page_text(corpus: Path, doc_id: str) -> dict[int, str]:
    pages = clean_document(extract_pages((corpus / f"{doc_id}.pdf").read_bytes()))
    return {page.number: page.text for page in pages}


def locate(text: str, quote: str) -> tuple[int, int] | None:
    """Span of quote in text, ignoring case and whitespace exactly as check_questions does."""
    folded: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        if not char.isspace():
            for part in char.casefold():
                folded.append(part)
                origin.append(index)
    target = "".join(char for char in quote if not char.isspace()).casefold()
    found = "".join(folded).find(target) if target else -1
    if found < 0:
        return None
    return origin[found], origin[found + len(target) - 1] + 1


def excerpt(text: str, span: tuple[int, int], width: int) -> str:
    start, end = span
    left, right = max(0, start - width), min(len(text), end + width)
    return (
        ("... " if left else "")
        + f"{text[left:start]}>>>{text[start:end]}<<<{text[end:right]}"
        + (" ..." if right < len(text) else "")
    )


def show(entry: dict[str, Any], corpus: Path, width: int) -> None:
    pages = ", ".join(str(page) for page in entry["expected_pages"]) or "-"
    print(
        f"\n{'=' * WRAP}\n{entry['id']}  [{entry['category']} | {entry['type']} | "
        f"{entry['difficulty']}]  review: {entry['review']}"
    )
    print(textwrap.fill(f"Q: {entry['question']}", WRAP, subsequent_indent="   "))
    print(
        f"Expected: {entry['expected_behavior']}   docs: {entry['expected_docs'] or '-'}   pages: {pages}"
    )
    print(textwrap.fill(f"Sketch: {entry['answer_sketch']}", WRAP, subsequent_indent="        "))
    if not entry["evidence"]:
        print("No evidence: the corpus has no answer, so the correct reply abstains.")
    for number, item in enumerate(entry["evidence"], start=1):
        text = page_text(corpus, item["doc_id"]).get(item["page"], "")
        span = locate(text, str(item["quote"]))
        print(f"\nEvidence {number} ({item['doc_id']} p{item['page']}):")
        if span is None:
            print("   quote not found on this page; run check_questions.py")
            continue
        print(textwrap.indent(textwrap.fill(excerpt(text, span, width), WRAP - 3), "   "))


def set_review(path: Path, qid: str, verdict: str) -> None:
    """Replace one question's review value in place, preserving comments and line endings."""
    with path.open(encoding="utf-8", newline="") as file:
        text = file.read()
    block = re.compile(
        rf"(^- id: {re.escape(qid)}\r?\n(?:(?!- id: ).*\n)*?[ ]+review: )[a-z]+", re.MULTILINE
    )
    updated, count = block.subn(rf"\g<1>{verdict}", text, count=1)
    if count != 1:
        raise RuntimeError(f"could not find the review field of {qid} in {path}")
    with path.open("w", encoding="utf-8", newline="") as file:
        file.write(updated)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--questions", type=Path, default=ROOT / "eval" / "questions.yaml")
    parser.add_argument("--corpus", type=Path, default=ROOT / "corpus" / "pdf")
    parser.add_argument("--ids", help="comma-separated ids, e.g. q03,u05")
    parser.add_argument("--all", action="store_true", help="include questions already reviewed")
    parser.add_argument(
        "--context", type=int, default=300, help="characters shown around each quote"
    )
    parser.add_argument(
        "--interactive", action="store_true", help="record a verdict after each question"
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    entries: list[dict[str, Any]] = yaml.safe_load(args.questions.read_text(encoding="utf-8"))
    wanted = set(args.ids.split(",")) if args.ids else None
    selected = [
        entry
        for entry in entries
        if (wanted is None or entry["id"] in wanted)
        and (args.all or wanted or entry["review"] == "pending")
    ]
    if wanted and (unknown := wanted - {entry["id"] for entry in entries}):
        print(f"unknown ids: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 1
    print(f"{len(selected)} question(s) to review")

    recorded = 0
    for entry in selected:
        show(entry, args.corpus, args.context)
        if not args.interactive:
            continue
        try:
            answer = input("\n[a]ccept  [d]rop  [s]kip  [q]uit > ").strip().lower()
        except EOFError:
            answer = "q"
        if answer == "q":
            break
        if answer in VERDICTS:
            set_review(args.questions, entry["id"], VERDICTS[answer])
            recorded += 1
    if args.interactive:
        print(f"\nrecorded {recorded} verdict(s); run check_questions.py to validate the file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
