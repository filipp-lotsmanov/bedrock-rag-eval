"""Text normalisation and fidelity metrics for the PDF parser comparison.

Both sides of the comparison (JATS ground truth and PDF extraction) pass
through the same normalisation, so strictness penalises every parser equally
and the comparison stays valid even where absolute recovery reads low.

Normalisation decisions, all driven by artefacts observed in the corpus:
    NFKC                  folds ligatures and Greek variants
    quote/dash folding    NFKC leaves U+2019/201C/2013 alone; PDF extraction
                          may yield either curly or ASCII forms
    double quotes dropped pysbd never splits between a pair of quotes, so one
                          unpaired quote swallows everything up to the next.
                          Observed in PMC12756423 (9 opening, 8 closing): one
                          "sentence" held 30% of the article.
    hyphen-break join     'hippo-\\ncampus' -> 'hippocampus'; line-break
                          hyphenation is a layout artefact, not a failure.
                          Letters only, so '10-\\n20' stays a range.
                          Soft hyphens (U+00AD) are joined and then dropped.
    bracket/punct spacing JATS xref boundaries leave '( Smith et al., 2021 )'
                          where the PDF renders '(Smith et al., 2021)'

Two stages, in this order:
    unwrap     whole document, before sentence splitting. pysbd treats every
               newline as a boundary, so layout line breaks must go first.
    normalise  per sentence, after splitting, so pysbd still sees original
               casing and punctuation.

Known limitations:
    Section titles merge into the following sentence, on both sides equally.
    A genuine compound hyphen at a line break ('beta-\\namyloid') is joined,
    since it cannot be told apart from syllable hyphenation.
    Matching is not one-to-one: two similar ground-truth sentences may match
    the same extracted sentence. Ties resolve to the first occurrence.
"""

import difflib
import math
import re
import unicodedata
from functools import lru_cache

import pysbd

MIN_SENTENCE_CHARS = 20
"""Sentences shorter than this are section titles, labels, or stray tokens."""

_PUNCT_FOLD = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": None,
        "\u201d": None,
        "\u201e": None,
        '"': None,
        "\u2010": "-",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)

_LETTER = r"[^\W\d_]"
_HYPHEN_BREAK = re.compile(rf"(?<={_LETTER})[-\u00ad][ \t]*\n\s*(?={_LETTER})")
_SOFT_HYPHEN = "\u00ad"
_WHITESPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,;:.)\]])")
_SPACE_AFTER_OPEN = re.compile(r"([(\[])\s+")
_SPACE_AROUND_HYPHEN = re.compile(r"(?<=\w)\s*-\s*(?=\w)")


def unwrap(text: str) -> str:
    """Remove layout line breaks from a whole document before splitting."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_PUNCT_FOLD)
    text = _HYPHEN_BREAK.sub("", text)
    text = text.replace(_SOFT_HYPHEN, "")
    return _WHITESPACE.sub(" ", text).strip()


def normalise(text: str) -> str:
    """Canonical comparison form for one sentence. Applied to both sides."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_PUNCT_FOLD)
    text = _WHITESPACE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _SPACE_AFTER_OPEN.sub(r"\1", text)
    text = _SPACE_AROUND_HYPHEN.sub("-", text)
    return text.strip().casefold()


@lru_cache(maxsize=1)
def _segmenter() -> pysbd.Segmenter:
    """Construction is expensive; one instance for the process."""
    return pysbd.Segmenter(language="en", clean=False)


def split_sentences(text: str) -> list[str]:
    """Unwrap the document, split it, then normalise each sentence.

    pysbd uses capitalisation and original punctuation as boundary signals,
    so casefolding happens only after splitting.
    """
    raw = _segmenter().segment(unwrap(text))
    out = []
    for sentence in raw:
        norm = normalise(sentence)
        if len(norm) >= MIN_SENTENCE_CHARS:
            out.append(norm)
    return out


def _length_bounds(length: int, threshold: float) -> tuple[float, float]:
    """Candidate lengths that can still reach the threshold.

    ratio() = 2M / (la + lb) and M <= min(la, lb), so for lb = k * la the
    best achievable ratio is 2k / (1 + k) when k <= 1 and 2 / (1 + k) when
    k >= 1. Solving each for >= threshold gives k >= t / (2 - t) and
    k <= (2 - t) / t.
    """
    if threshold <= 0:
        return 0.0, float("inf")
    return length * threshold / (2 - threshold), length * (2 - threshold) / threshold


def _best_match(needle: str, haystack: list[str], threshold: float) -> int | None:
    """Index of the closest sentence above threshold, or None.

    Length blocking and quick_ratio() prune the search: a full ratio() over
    every pair is O(n*m) on strings of a few hundred characters, which is
    prohibitive at ~400 sentences per side. Both prunes are upper bounds on
    ratio(), so neither can discard a true match.

    Ties go to the earliest candidate, so repeated text (running headers,
    boilerplate) always resolves to its first occurrence.
    """
    lo, hi = _length_bounds(len(needle), threshold)

    best_score = threshold
    best_index = None
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(needle)

    for index, candidate in enumerate(haystack):
        if not lo <= len(candidate) <= hi:
            continue
        matcher.set_seq1(candidate)
        if matcher.quick_ratio() < best_score:
            continue
        score = matcher.ratio()
        accept = score >= threshold if best_index is None else score > best_score
        if accept:
            best_score = score
            best_index = index
            if score == 1.0:
                break

    return best_index


def recovery(truth: list[str], extracted: list[str], threshold: float) -> tuple[float, list[int]]:
    """Fraction of ground-truth sentences found, plus their matched positions.

    The position list feeds order_score; both come from one pass so that the
    order metric is computed over exactly the matched subset.
    """
    if not truth:
        return 0.0, []

    matched: list[int] = []
    for sentence in truth:
        index = _best_match(sentence, extracted, threshold)
        if index is not None:
            matched.append(index)

    return len(matched) / len(truth), matched


def _ranks(values: list[float]) -> list[float]:
    """Average ranks, so ties are handled correctly."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = mean_rank
        i = j + 1
    return out


def order_score(matched: list[int]) -> float:
    """Spearman correlation between ground-truth order and extracted order.

    1.0 means reading order preserved. A parser that interleaves two columns
    scores well below that even with high recovery, which is the failure this
    metric exists to catch.

    Computed over matched sentences only, so it must always be reported
    alongside recovery - a parser that recovers little gets a correlation
    from a small sample.
    """
    n = len(matched)
    if n < 2:
        return float("nan")

    x = _ranks([float(i) for i in range(n)])
    y = _ranks([float(v) for v in matched])

    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=True))
    var_x = sum((a - mean_x) ** 2 for a in x)
    var_y = sum((b - mean_y) ** 2 for b in y)

    if var_x == 0 or var_y == 0:
        return float("nan")

    return cov / math.sqrt(var_x * var_y)