"""Inspect JATS ground-truth extraction on the real corpus.

Unit tests cover synthetic cases; this checks the actual articles for
extraction artefacts before any parser is scored against them.

Usage:
    uv run python scripts/inspect_ground_truth.py
    uv run python scripts/inspect_ground_truth.py --examples 5
"""

import argparse
import csv
import re
import statistics
from pathlib import Path

from bakeoff import extract_ground_truth, split_sentences

ARTEFACTS = {
    "space_before_punct": re.compile(r"\s[,.;:)\]]"),
    "space_after_open": re.compile(r"[(\[]\s"),
    "glued_sentence": re.compile(r"[a-z]\.[A-Z][a-z]"),
    "glued_camel": re.compile(r"[a-z]{3}[A-Z][a-z]{2}"),
    "entity_residue": re.compile(r"&(?:amp|lt|gt|quot|#\d+);"),
    "markup_residue": re.compile(r"<[a-z/]"),
    "replacement_char": re.compile("\ufffd"),
    "private_use": re.compile("[\ue000-\uf8ff]"),
}
"""Patterns that signal extraction problems. glued_camel also fires on
legitimate names (McDonald, iPhone): read its examples, don't trust its count."""

FORMULA = re.compile(r"<(?:\w+:)?(inline-formula|disp-formula)[\s>]")


def context(text: str, match: re.Match[str], width: int = 30) -> str:
    start = max(match.start() - width, 0)
    return repr(text[start : match.end() + width])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="corpus")
    parser.add_argument("--examples", type=int, default=2)
    args = parser.parse_args()

    corpus = Path(args.corpus)
    with (corpus / "manifest.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    for row in rows:
        xml_path = corpus / "xml" / f"{row['pmcid']}.xml"
        raw_xml = xml_path.read_text(encoding="utf-8")
        text = extract_ground_truth(xml_path)
        sentences = split_sentences(text)
        lengths = [len(s) for s in sentences]

        print(f"\n=== {row['pmcid']}  {text[:60]!r}")
        if not sentences:
            print(f"  NO SENTENCES  chars={len(text)}")
            continue
        print(
            f"chars={len(text)}  sentences={len(sentences)}  "
            f"len min/median/max={min(lengths)}/{statistics.median(lengths):.0f}"
            f"/{max(lengths)}  formulas_pruned={len(FORMULA.findall(raw_xml))}"
        )

        for name, pattern in ARTEFACTS.items():
            matches = list(pattern.finditer(text))
            if matches:
                print(f"  {name}: {len(matches)}")
                for match in matches[: args.examples]:
                    print(f"      {context(text, match)}")

        longest = max(sentences, key=len)
        print(f"  longest sentence: {longest[:150]!r}...")


if __name__ == "__main__":
    main()
