import threading
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pytest

import ingest.chunk as chunk_module
from ingest.chunk import (
    ChunkingConfig,
    ChunkingError,
    Document,
    Sentence,
    build_chunks,
    check_ranges,
    check_sentences,
    chunk,
    join_pages,
    limit_length,
    pack,
    split_sentences,
)
from ingest.clean import CleanPage, clean_document
from ingest.extract import extract_pages

CONFIG = ChunkingConfig(target_chars=50, overlap_sentences=1, max_chars=100)


# Plumbing tests: complete.


def test_join_pages_records_offsets() -> None:
    document = join_pages([CleanPage(1, "First page."), CleanPage(3, "Third page.")])
    assert document.text == "First page. Third page."
    assert document.page_starts == [0, 12]
    assert document.page_numbers == [1, 3]


def test_join_pages_keeps_hyphen_across_page_break() -> None:
    document = join_pages([CleanPage(1, "a dopamine-"), CleanPage(2, "producing enzyme")])
    assert document.text == "a dopamine-producing enzyme"
    assert document.page_starts == [0, 11]


def test_page_at_boundaries() -> None:
    document = Document("First page. Third page.", [0, 12], [1, 3])
    assert document.page_at(0) == 1
    assert document.page_at(11) == 1
    assert document.page_at(12) == 3
    assert document.page_at(22) == 3


def test_split_sentences_offsets_exclude_whitespace() -> None:
    text = "DSV increased levels (p < 0.05). Fig. 2 shows e.g. this.  Short."
    sentences = split_sentences(text)
    assert [text[s.start : s.end] for s in sentences] == [
        "DSV increased levels (p < 0.05).",
        "Fig. 2 shows e.g. this.",
        "Short.",
    ]


def test_split_sentences_handles_repeated_sentences() -> None:
    text = "Same sentence. Same sentence. Same sentence."
    sentences = split_sentences(text)
    assert [s.start for s in sentences] == [0, 15, 30]


def test_split_sentences_clips_overlapping_pysbd_spans() -> None:
    # pysbd returns overlapping spans for this text: (0, 8) then (6, 10).
    text = "\u03b1-syn ! ! ! Inc."
    sentences = split_sentences(text)
    assert all(a.end <= b.start for a, b in pairwise(sentences))
    check_sentences(text, sentences, CONFIG)
    assert chunk([CleanPage(1, text)], CONFIG)


def test_config_rejects_inconsistent_values() -> None:
    with pytest.raises(ValueError):
        ChunkingConfig(target_chars=200, overlap_sentences=1, max_chars=100)
    with pytest.raises(ValueError):
        ChunkingConfig(target_chars=50, overlap_sentences=-1, max_chars=100)


def test_check_sentences_rejects_overlong_and_unordered() -> None:
    text = "x" * 300
    with pytest.raises(ChunkingError):
        check_sentences(text, [Sentence(0, 150)], CONFIG)
    with pytest.raises(ChunkingError):
        check_sentences(text, [Sentence(10, 20), Sentence(5, 30)], CONFIG)


SENTENCES = [Sentence(0, 10), Sentence(11, 20), Sentence(21, 30), Sentence(31, 40)]


def test_check_ranges_accepts_overlapping_cover() -> None:
    check_ranges(SENTENCES, [range(2), range(1, 3), range(2, 4)], CONFIG)


@pytest.mark.parametrize(
    "ranges",
    [
        [],
        [range(2)],
        [range(1, 4)],
        [range(2), range(3, 4)],
        [range(2), range(4)],
        [range(2), range(2, 2), range(2, 4)],
        [range(0, 4, 2)],
    ],
    ids=[
        "none",
        "misses-end",
        "misses-start",
        "gap",
        "no-progress",
        "empty",
        "stepped",
    ],
)
def test_check_ranges_rejects_broken_contracts(ranges: list[range]) -> None:
    with pytest.raises(ChunkingError):
        check_ranges(SENTENCES, ranges, CONFIG)


def test_check_ranges_rejects_overlong_chunk() -> None:
    sentences = [Sentence(0, 60), Sentence(61, 120)]
    with pytest.raises(ChunkingError):
        check_ranges(sentences, [range(2)], CONFIG)


def test_build_chunks_slices_text_and_spans_pages() -> None:
    document = join_pages(
        [CleanPage(4, "Ends here. Starts"), CleanPage(5, "on the next page. Done.")]
    )
    sentences = split_sentences(document.text)
    chunks = build_chunks(document, sentences, [range(1), range(1, 3)])
    assert [(c.index, c.text, c.page_start, c.page_end) for c in chunks] == [
        (0, "Ends here.", 4, 4),
        (1, "Starts on the next page. Done.", 4, 5),
    ]


def spaced(lengths: list[int]) -> list[Sentence]:
    """Sentences of the given lengths, separated by one character."""
    sentences, offset = [], 0
    for length in lengths:
        sentences.append(Sentence(offset, offset + length))
        offset += length + 1
    return sentences


def span(sentences: list[Sentence], r: range) -> int:
    return sentences[r.stop - 1].end - sentences[r.start].start


def run_with_timeout(function: Callable[[], list[range]], seconds: float = 2.0) -> list[range]:
    """Run function in a daemon thread so an infinite loop fails the test instead of hanging it.

    If function raises, pytest reports the traceback as an unhandled-thread warning.
    """
    result: list[list[range]] = []
    thread = threading.Thread(target=lambda: result.append(function()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "pack() did not finish: it is not making progress"
    assert result, "pack() raised in the worker thread; see the thread-exception warning"
    return result[0]


def test_pack_respects_target_size() -> None:
    config = ChunkingConfig(target_chars=100, overlap_sentences=0, max_chars=400)
    sentences = spaced([30, 40, 20, 50, 10, 90, 35, 35, 60, 15])
    ranges = pack(sentences, config)
    check_ranges(sentences, ranges, config)
    for r in ranges:
        assert span(sentences, r) <= config.target_chars or len(r) == 1
    for r in ranges[:-1]:
        assert span(sentences, range(r.start, r.stop + 1)) > config.target_chars


def test_pack_fills_up_to_exactly_target() -> None:
    config = ChunkingConfig(target_chars=100, overlap_sentences=0, max_chars=400)
    sentences = spaced([49, 50, 10])
    assert pack(sentences, config) == [range(2), range(2, 3)]


def test_pack_overlap_is_exactly_configured() -> None:
    config = ChunkingConfig(target_chars=100, overlap_sentences=2, max_chars=400)
    sentences = spaced([20] * 20)
    ranges = pack(sentences, config)
    check_ranges(sentences, ranges, config)
    for previous, current in pairwise(ranges):
        assert previous.stop - current.start == min(config.overlap_sentences, len(previous) - 1)


def test_pack_makes_progress_with_single_sentence_chunks() -> None:
    config = ChunkingConfig(target_chars=50, overlap_sentences=1, max_chars=200)
    sentences = spaced([50] * 5)
    ranges = run_with_timeout(lambda: pack(sentences, config))
    assert ranges == [range(i, i + 1) for i in range(5)]


def test_pack_zero_overlap_partitions_sentences() -> None:
    config = ChunkingConfig(target_chars=60, overlap_sentences=0, max_chars=200)
    sentences = spaced([25, 25, 25, 10, 40, 30, 5, 55])
    ranges = pack(sentences, config)
    check_ranges(sentences, ranges, config)
    for previous, current in pairwise(ranges):
        assert current.start == previous.stop


def test_pack_drops_overlap_that_would_exceed_max() -> None:
    config = ChunkingConfig(target_chars=1000, overlap_sentences=1, max_chars=1600)
    sentences = spaced([100, 800, 900, 50])
    ranges = pack(sentences, config)
    check_ranges(sentences, ranges, config)
    assert ranges == [range(2), range(2, 4)]


def test_limit_length_leaves_short_sentences_alone() -> None:
    config = ChunkingConfig(target_chars=10, overlap_sentences=0, max_chars=20)
    text = "Short one. Another short."
    sentences = split_sentences(text)
    assert limit_length(text, sentences, config) == sentences


def test_limit_length_splits_at_whitespace() -> None:
    config = ChunkingConfig(target_chars=10, overlap_sentences=0, max_chars=20)
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa."
    pieces = limit_length(text, [Sentence(0, len(text))], config)
    check_sentences(text, pieces, config)
    assert all(p.length <= config.max_chars for p in pieces)
    assert " ".join(text[p.start : p.end] for p in pieces) == text
    assert all(text[p.end] == " " for p in pieces[:-1])


def test_limit_length_hard_splits_without_whitespace() -> None:
    config = ChunkingConfig(target_chars=10, overlap_sentences=0, max_chars=20)
    text = "Participantswereassignedtoanexperimentalgroupwhich."
    pieces = limit_length(text, [Sentence(0, len(text))], config)
    check_sentences(text, pieces, config)
    assert [p.length for p in pieces] == [20, 20, len(text) - 40]
    assert "".join(text[p.start : p.end] for p in pieces) == text


CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "pdf"
corpus_pdfs = sorted(CORPUS.glob("*.pdf")) if CORPUS.is_dir() else []
DEFAULT_CONFIG = getattr(chunk_module, "DEFAULT_CONFIG", None)


@pytest.mark.skipif(not corpus_pdfs, reason="corpus PDFs are not in the repository")
@pytest.mark.skipif(DEFAULT_CONFIG is None, reason="DEFAULT_CONFIG is not set yet")
@pytest.mark.parametrize("pdf", corpus_pdfs, ids=lambda path: path.stem)
def test_chunk_corpus_document(pdf: Path) -> None:
    assert DEFAULT_CONFIG is not None
    pages = clean_document(extract_pages(pdf.read_bytes()))
    chunks = chunk(pages, DEFAULT_CONFIG)
    page_numbers = {page.number for page in pages}

    assert chunks
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert len(c.text) <= DEFAULT_CONFIG.max_chars
        assert c.page_start <= c.page_end
        assert {c.page_start, c.page_end} <= page_numbers
    document = join_pages(pages)
    for sentence in limit_length(document.text, split_sentences(document.text), DEFAULT_CONFIG):
        assert any(document.text[sentence.start : sentence.end] in c.text for c in chunks)
