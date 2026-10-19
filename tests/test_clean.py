import re
from pathlib import Path

import pytest

from ingest.clean import (
    CleanPage,
    CleanupError,
    clean_document,
    cut_back_matter,
    expand_ligatures,
    map_glyph_digits,
    remove_boilerplate,
    remove_sidebar,
    unwrap,
)
from ingest.extract import extract_pages

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "pdf"

FOOTER = "Frontiers in Neuroscience {n:02d} frontiersin.org"
HEADER = "Singh et al. 10.3389/fnins.2025.1672793"
LICENCE_END = [
    "No use, distribution or reproduction",
    "is permitted which does not comply with",
    "these terms.",
]


def test_glyph_digits_map_to_numbers() -> None:
    assert (
        map_glyph_digits("Fang Qiu/one.tnum, Xiaodong Liu /two.tnum*")
        == "Fang Qiu1, Xiaodong Liu 2*"
    )
    assert map_glyph_digits("DOI /one.tnum/zero.tnum./three.tnum") == "DOI 10.3"


def test_glyph_digits_leave_urls_and_dois_alone() -> None:
    for line in ("doi: 10.1016/j.neuron.2020.01.001", "https://example.org/one.html"):
        assert map_glyph_digits(line) == line


def test_ligatures_expand() -> None:
    assert (
        expand_ligatures("speci\ufb01c di\ufb00erent In\ufb02uence")
        == "specific different Influence"
    )
    assert (
        expand_ligatures("e\ue013ect di\ue013usion-weighted")
        == "effect diffusion-weighted"
    )


def test_boilerplate_removes_repeating_edge_lines_only() -> None:
    middle = ["body a", "body b", "Repeated in the middle", "body c", "body d"]
    pages = [
        [HEADER, FOOTER.format(n=n), f"page {n} start", *middle, f"page {n} end"]
        for n in range(1, 5)
    ]
    cleaned = remove_boilerplate(pages)
    for lines in cleaned:
        assert HEADER not in lines
        assert not any(line.startswith("Frontiers in Neuroscience") for line in lines)
        assert "Repeated in the middle" in lines


def test_boilerplate_needs_at_least_two_pages() -> None:
    page = [FOOTER.format(n=1), "only page"]
    assert remove_boilerplate([page]) == [page]


def test_sidebar_at_end_of_page() -> None:
    page = [
        "Title line",
        "Abstract text.",
        "OPEN ACCESS",
        "EDITED BY",
        "Someone",
        *LICENCE_END,
    ]
    page += [
        "TYPE Original Research",
        "PUBLISHED 18 December 2025",
        "DOI 10.3389/fnins.2025.1672793",
    ]
    assert remove_sidebar(page) == ["Title line", "Abstract text."]


def test_sidebar_at_start_with_licence_split_across_lines() -> None:
    page = [
        "TYPE Original Research",
        "PUBLISHED 29 December 2025",
        "DOI 10.3389/fnins.2025.1750792",
    ]
    page += [
        "OPEN ACCESS",
        "COPYRIGHT",
        "permitted which does not comply with these",
        "terms.",
        "Influence of electrode",
    ]
    assert remove_sidebar(page) == ["Influence of electrode"]


def test_sidebar_without_licence_end_raises() -> None:
    with pytest.raises(CleanupError):
        remove_sidebar(["OPEN ACCESS", "EDITED BY", "Someone"])


def test_sidebar_missing_raises() -> None:
    with pytest.raises(CleanupError):
        remove_sidebar(["Title", "Abstract."])


def test_back_matter_cut_at_first_heading() -> None:
    pages = [
        ["Data availability statement"],
        ["body", "end of discussion."],
        ["Publisher\u2019s note", "x"],
        ["References"],
    ]
    assert cut_back_matter(pages) == [
        ["Data availability statement"],
        ["body", "end of discussion."],
        [],
    ]


def test_back_matter_ignores_page_one_and_partial_matches() -> None:
    pages = [
        ["Funding"],
        ["funding number is GR0244415.", "text"],
        ["Author contributions", "x"],
    ]
    assert cut_back_matter(pages) == [
        ["Funding"],
        ["funding number is GR0244415.", "text"],
        [],
    ]


def test_back_matter_missing_raises() -> None:
    with pytest.raises(CleanupError):
        cut_back_matter([["a"], ["b"]])


def test_unwrap_keeps_line_end_hyphens() -> None:
    assert (
        unwrap(["Various \u03b1-syn-", "aggregation-inducing stimuli"])
        == "Various \u03b1-syn-aggregation-inducing stimuli"
    )


def test_unwrap_collapses_whitespace() -> None:
    assert (
        unwrap(["Parkinsonism Relat. Disord.  81,", "84\u201388."])
        == "Parkinsonism Relat. Disord. 81, 84\u201388."
    )


def test_clean_document_keeps_page_numbers_and_drops_empty_pages() -> None:
    first = ["Title.", "OPEN ACCESS", *LICENCE_END]
    pages = [first, ["Body on page two."], ["Data availability statement", "x"]]
    result = clean_document(["\n".join(lines) for lines in pages])
    assert result == [CleanPage(1, "Title."), CleanPage(2, "Body on page two.")]


corpus_pdfs = sorted(CORPUS.glob("*.pdf")) if CORPUS.is_dir() else []
LEFTOVERS = re.compile(
    r"OPEN ACCESS|COPYRIGHT|\.tnum|[\ufb00-\ufb04\ue013]|Frontiers ?in Neuroscience \d+ frontiersin\.org"
)


@pytest.mark.skipif(not corpus_pdfs, reason="corpus PDFs are not in the repository")
@pytest.mark.parametrize("pdf", corpus_pdfs, ids=lambda path: path.stem)
def test_corpus_document_cleans(pdf: Path) -> None:
    raw = extract_pages(pdf.read_bytes())
    pages = clean_document(raw)
    text = " ".join(page.text for page in pages)

    assert LEFTOVERS.search(text) is None
    assert "Data availability statement" not in text
    assert [page.number for page in pages] == sorted({page.number for page in pages})
    assert 0.5 < len(text) / sum(len(page) for page in raw) < 0.95