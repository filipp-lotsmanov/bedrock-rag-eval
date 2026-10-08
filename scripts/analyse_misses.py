"""List the ground-truth sentences a parser fails to recover, and why.

For each unmatched sentence, finds the closest extracted sentence and sorts
the miss into a band:
    near   best score in [--near, threshold): text is there but differs.
           The character-level differences are shown, and the most common
           ones are tallied, because systematic differences (diacritics,
           ligatures, spacing) show up as repeated substitutions.
    far    best score below --near, or no candidate: text is missing,
           merged with page furniture, or split differently.

Usage:
    uv run python scripts/analyse_misses.py PMC12756423 PMC12794296
    uv run python scripts/analyse_misses.py PMC12756423 --threshold 0.99 --examples 8
"""

import argparse
import difflib
from collections import Counter
from pathlib import Path

from bakeoff import extract_ground_truth, recovery, split_sentences
from bakeoff.fidelity import _best_match
from bakeoff.parsers import PARSERS


def closest(sentence: str, extracted: list[str], floor: float) -> tuple[float, str]:
    """Best extracted candidate scoring at least floor, as (score, text)."""
    index = _best_match(sentence, extracted, floor)
    if index is None:
        return 0.0, ""
    candidate = extracted[index]
    return difflib.SequenceMatcher(None, candidate, sentence, autojunk=False).ratio(), candidate


def differences(truth: str, candidate: str) -> list[tuple[str, str]]:
    """(truth segment, extracted segment) for every non-equal stretch."""
    matcher = difflib.SequenceMatcher(None, truth, candidate, autojunk=False)
    return [
        (truth[i1:i2], candidate[j1:j2])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def show(segments: list[tuple[str, str]], limit: int = 6) -> str:
    return "  ".join(f"[{a!r} -> {b!r}]" for a, b in segments[:limit])


def analyse(pmcid: str, args: argparse.Namespace) -> None:
    corpus = Path(args.corpus)
    truth = split_sentences(extract_ground_truth(corpus / "xml" / f"{pmcid}.xml"))
    pages = PARSERS[args.parser](corpus / "pdf" / f"{pmcid}.pdf")
    extracted = split_sentences("\n".join(pages))

    fraction, _ = recovery(truth, extracted, args.threshold)
    near: list[tuple[float, str, str]] = []
    far: list[tuple[float, str, str]] = []
    for sentence in truth:
        if _best_match(sentence, extracted, args.threshold) is not None:
            continue
        score, candidate = closest(sentence, extracted, args.floor)
        (near if score >= args.near else far).append((score, sentence, candidate))

    print(f"\n=== {pmcid}  parser={args.parser}  threshold={args.threshold}")
    print(
        f"truth={len(truth)}  recovered={fraction:.3f}  "
        f"near_misses={len(near)}  far_misses={len(far)}"
    )

    tally: Counter[tuple[str, str]] = Counter()
    for _, sentence, candidate in near:
        tally.update(differences(sentence, candidate))
    if tally:
        print("most common differences (truth -> extracted):")
        for (a, b), n in tally.most_common(args.top):
            print(f"  {n:>4}x  {a!r} -> {b!r}")

    print(f"near misses (closest first, {args.examples} shown):")
    for score, sentence, candidate in sorted(near, reverse=True)[: args.examples]:
        print(f"  {score:.3f}  {sentence[:110]!r}")
        print(f"         {show(differences(sentence, candidate))}")

    print(f"far misses ({args.examples} shown):")
    for score, sentence, candidate in far[: args.examples]:
        best = f"best {score:.3f}: {candidate[:80]!r}" if candidate else "no candidate"
        print(f"  {sentence[:110]!r}")
        print(f"         {best}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pmcids", nargs="+")
    parser.add_argument("--corpus", default="corpus")
    parser.add_argument("--parser", default="pypdf", choices=sorted(PARSERS))
    parser.add_argument("--threshold", type=float, default=0.95)
    parser.add_argument("--near", type=float, default=0.80)
    parser.add_argument("--floor", type=float, default=0.50, help="ignore candidates below this")
    parser.add_argument("--examples", type=int, default=5)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    for pmcid in args.pmcids:
        analyse(pmcid, args)


if __name__ == "__main__":
    main()
