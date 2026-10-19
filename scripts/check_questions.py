"""Validate eval/questions.yaml against the cleaned corpus text.

Checks the schema, the rules per category, and that every evidence quote occurs on the
page it cites, using the same extract and clean steps as the ingest Lambda. Prints the
coverage per paper, type, difficulty and category. Exits 1 on any error, or with --strict
on any question still pending review.

    uv run --group ingest python scripts/check_questions.py
"""

import argparse
import re
import sys
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from ingest.clean import clean_document
from ingest.extract import extract_pages

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "id",
    "question",
    "category",
    "expected_behavior",
    "expected_docs",
    "expected_pages",
    "answer_sketch",
    "evidence",
    "type",
    "difficulty",
    "source",
    "review",
)
BEHAVIOR = {
    "answerable": "answer",
    "not_covered": "abstain",
    "off_topic": "abstain",
    "false_premise": "correct_premise",
}
TYPES = {"factoid", "numeric", "paraphrase", "multi_passage", "unanswerable"}
DIFFICULTIES = {"easy", "medium", "hard"}
REVIEWS = {"pending", "accepted", "edited", "dropped"}
ID_PATTERN = re.compile(r"[qu]\d{2}")


def normalize(text: str) -> str:
    """Case and whitespace are ignored, so quotes also match pages with lost word spacing."""
    return re.sub(r"\s+", "", text).casefold()


@cache
def cleaned_pages(corpus: Path, doc_id: str) -> dict[int, str]:
    pages = clean_document(extract_pages((corpus / f"{doc_id}.pdf").read_bytes()))
    return {page.number: normalize(page.text) for page in pages}


def check_entry(entry: dict[str, Any], corpus: Path) -> list[str]:
    qid = entry.get("id", "?")
    missing = [key for key in REQUIRED if key not in entry]
    if missing:
        return [f"{qid}: missing {missing}"]
    errors: list[str] = []
    category, docs, pages, evidence = (
        entry["category"],
        entry["expected_docs"],
        entry["expected_pages"],
        entry["evidence"],
    )
    if not ID_PATTERN.fullmatch(str(qid)):
        errors.append(f"{qid}: id must look like q01 or u01")
    if category not in BEHAVIOR:
        return [*errors, f"{qid}: unknown category {category!r}"]
    if entry["expected_behavior"] != BEHAVIOR[category]:
        errors.append(f"{qid}: {category} needs expected_behavior {BEHAVIOR[category]!r}")
    if entry["type"] not in TYPES or (entry["type"] == "unanswerable") != (
        category != "answerable"
    ):
        errors.append(f"{qid}: type {entry['type']!r} does not fit category {category!r}")
    if entry["difficulty"] not in DIFFICULTIES:
        errors.append(f"{qid}: unknown difficulty {entry['difficulty']!r}")
    if entry["review"] not in REVIEWS:
        errors.append(f"{qid}: unknown review status {entry['review']!r}")
    if not str(entry["question"]).strip() or not str(entry["answer_sketch"]).strip():
        errors.append(f"{qid}: question and answer_sketch must not be empty")
    needs_evidence = category in {"answerable", "false_premise"}
    if needs_evidence and not (docs and pages and evidence):
        errors.append(f"{qid}: {category} needs expected_docs, expected_pages and evidence")
    if not needs_evidence and (docs or pages or evidence):
        errors.append(
            f"{qid}: {category} must have empty expected_docs, expected_pages and evidence"
        )
    for item in evidence:
        doc_id, page, quote = item.get("doc_id"), item.get("page"), str(item.get("quote", ""))
        if doc_id not in docs or page not in pages:
            errors.append(
                f"{qid}: evidence {doc_id} p{page} is not in expected_docs/expected_pages"
            )
            continue
        if not (corpus / f"{doc_id}.pdf").exists():
            errors.append(f"{qid}: {doc_id}.pdf not found in {corpus}")
            continue
        text = cleaned_pages(corpus, doc_id)
        if page not in text:
            errors.append(
                f"{qid}: {doc_id} has no cleaned page {page} (pages {min(text)}-{max(text)})"
            )
        elif len(normalize(quote)) < 10 or normalize(quote) not in text[page]:
            errors.append(f"{qid}: quote not found on {doc_id} p{page}: {quote!r}")
    return errors


def report(entries: list[dict[str, Any]]) -> list[str]:
    active = [e for e in entries if e.get("review") != "dropped"]
    answerable = [e for e in active if e.get("category") == "answerable"]
    print(
        f"{len(active)} active questions ({len(entries) - len(active)} dropped): "
        f"{len(answerable)} answerable, {len(active) - len(answerable)} not"
    )
    for field in ("category", "type", "difficulty", "review"):
        print(f"  {field:<10} {dict(Counter(str(e.get(field)) for e in active))}")
    per_doc = Counter(doc for e in answerable for doc in e.get("expected_docs", []))
    print(f"  per paper  {dict(sorted(per_doc.items()))}")
    warnings = [
        f"type {t!r} has no question"
        for t in sorted(TYPES)
        if t not in {e.get("type") for e in active}
    ]
    warnings += [
        f"category {c!r} has no question"
        for c in sorted(BEHAVIOR)
        if c not in {e.get("category") for e in active}
    ]
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--questions", type=Path, default=ROOT / "eval" / "questions.yaml")
    parser.add_argument("--corpus", type=Path, default=ROOT / "corpus" / "pdf")
    parser.add_argument(
        "--strict", action="store_true", help="fail while any question is pending review"
    )
    args = parser.parse_args()

    entries = yaml.safe_load(args.questions.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        print("questions file must be a list of mappings", file=sys.stderr)
        return 1
    errors = [
        f"duplicate id {i}" for i, n in Counter(e.get("id") for e in entries).items() if n > 1
    ]
    for entry in entries:
        errors += check_entry(entry, args.corpus)
    warnings = report(entries)
    pending = [e["id"] for e in entries if e.get("review") == "pending"]
    if args.strict and pending:
        errors.append(f"{len(pending)} question(s) pending review: {', '.join(pending)}")
    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print("OK" if not errors else f"{len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
