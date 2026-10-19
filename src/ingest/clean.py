"""Cleanup of pypdf output for the Frontiers in Neuroscience corpus.

Every rule was derived from inspecting all 10 corpus PDFs. Rule numbers match the spec:
C7 and C8 normalise characters, C1 removes running headers and footers, C2 removes the
page-1 sidebar, C3 cuts the back matter, C4 and C5 unwrap lines into plain text.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

Page = list[str]

DIGIT_NAMES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)
_DIGIT_GLYPH = re.compile(r"/(" + "|".join(DIGIT_NAMES) + r")\.tnum")
# U+E013 is a private-use code some corpus fonts use for "ff" (e\ue013ect, di\ue013usion).
_LIGATURES = str.maketrans(
    {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ue013": "ff",
    }
)
_DIGITS = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")

EDGE_LINES = 3
SIDEBAR_START = "OPEN ACCESS"
SIDEBAR_END = "theseterms."
_SIDEBAR_HEADER = re.compile(r"^(TYPE |PUBLISHED |DOI 10\.)")
BACK_MATTER = frozenset(
    {
        "data availability statement",
        "ethics statement",
        "author contributions",
        "funding",
        "acknowledgments",
        "acknowledgements",
        "conflict of interest",
        "generative ai statement",
        "publisher's note",
        "supplementary material",
        "references",
    }
)


class CleanupError(ValueError):
    """A document does not match the layout the cleanup rules were derived from."""


@dataclass(frozen=True)
class CleanPage:
    number: int
    text: str


def map_glyph_digits(line: str) -> str:
    """C7: replace glyph names such as /two.tnum with the digit they stand for."""
    return _DIGIT_GLYPH.sub(lambda match: str(DIGIT_NAMES.index(match.group(1))), line)


def expand_ligatures(line: str) -> str:
    """C8: replace ligature characters, including the private-use "ff" glyph, with plain letters."""
    return line.translate(_LIGATURES)


def remove_boilerplate(pages: list[Page]) -> list[Page]:
    """C1: drop edge lines that repeat, ignoring digits, on at least half the pages."""

    def edge(lines: Page) -> Page:
        return lines[:EDGE_LINES] + lines[-EDGE_LINES:]

    pages_per_key = Counter(
        key
        for lines in pages
        for key in {_DIGITS.sub("#", line) for line in edge(lines)}
    )
    threshold = max(2, math.ceil(len(pages) / 2))
    boilerplate = {key for key, count in pages_per_key.items() if count >= threshold}

    cleaned = []
    for lines in pages:
        last = len(lines) - EDGE_LINES
        cleaned.append(
            [
                line
                for i, line in enumerate(lines)
                if not (
                    (i < EDGE_LINES or i >= last)
                    and _DIGITS.sub("#", line) in boilerplate
                )
            ]
        )
    return cleaned


def remove_sidebar(first_page: Page) -> Page:
    """C2: drop the article-info lines and the block from OPEN ACCESS to the licence's last line."""
    if SIDEBAR_START not in first_page:
        raise CleanupError(f"no {SIDEBAR_START!r} line on page 1")
    start = first_page.index(SIDEBAR_START)
    end = next(
        (
            i
            for i in range(start + 1, len(first_page))
            if (first_page[i - 1] + first_page[i])
            .replace(" ", "")
            .lower()
            .endswith(SIDEBAR_END)
        ),
        None,
    )
    if end is None:
        raise CleanupError("sidebar has no closing licence line")
    kept = first_page[:start] + first_page[end + 1 :]
    return [line for line in kept if not _SIDEBAR_HEADER.match(line)]


def cut_back_matter(pages: list[Page]) -> list[Page]:
    """C3: drop everything from the first back-matter heading, searching from page 2."""
    for p in range(1, len(pages)):
        for i, line in enumerate(pages[p]):
            if line.lower().replace("\u2019", "'") in BACK_MATTER:
                return [*pages[:p], pages[p][:i]]
    raise CleanupError("no back-matter heading found")


def unwrap(lines: Page) -> str:
    """C4 and C5: join wrapped lines, keeping line-end hyphens, and collapse whitespace."""
    parts: list[str] = []
    for line in lines:
        if parts and parts[-1].endswith("-"):
            parts[-1] += line
        else:
            parts.append(line)
    return _WHITESPACE.sub(" ", " ".join(parts)).strip()


def clean_document(page_texts: list[str]) -> list[CleanPage]:
    """Apply C7, C8, C1, C2, C3 and C4-C5 in order; drop pages left empty."""
    if not page_texts:
        raise CleanupError("document has no pages")
    pages = [
        [
            stripped
            for line in text.splitlines()
            if (stripped := expand_ligatures(map_glyph_digits(line)).strip())
        ]
        for text in page_texts
    ]
    pages = remove_boilerplate(pages)
    pages[0] = remove_sidebar(pages[0])
    pages = cut_back_matter(pages)
    cleaned = (
        CleanPage(number, unwrap(lines)) for number, lines in enumerate(pages, start=1)
    )
    return [page for page in cleaned if page.text]