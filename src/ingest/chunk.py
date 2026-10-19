"""Sentence-packed chunking of cleaned pages.

Pages are joined with character offsets, split into sentences with pysbd, length-limited,
packed into chunks, validated, and assembled with page ranges. The strategy (DEFAULT_CONFIG,
limit_length() and pack()) implements rules S1-S4, specified in tests/test_chunk.py.
"""

import bisect
from collections.abc import Sequence
from dataclasses import dataclass

import pysbd

from ingest.clean import CleanPage


class ChunkingError(ValueError):
    """The chunking strategy produced output that breaks the chunk contract."""


@dataclass(frozen=True)
class ChunkingConfig:
    target_chars: int
    overlap_sentences: int
    max_chars: int

    def __post_init__(self) -> None:
        if not 0 < self.target_chars <= self.max_chars:
            raise ValueError("need 0 < target_chars <= max_chars")
        if self.overlap_sentences < 0:
            raise ValueError("overlap_sentences must be >= 0")


# Strategy v1 baseline, provisional until the week-3 ablation.
DEFAULT_CONFIG = ChunkingConfig(target_chars=1500, overlap_sentences=1, max_chars=3000)


@dataclass(frozen=True)
class Sentence:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    page_start: int
    page_end: int


@dataclass(frozen=True)
class Document:
    text: str
    page_starts: list[int]
    page_numbers: list[int]

    def page_at(self, offset: int) -> int:
        """The page number that holds the character at offset."""
        return self.page_numbers[bisect.bisect_right(self.page_starts, offset) - 1]


def join_pages(pages: Sequence[CleanPage]) -> Document:
    """Concatenate page texts, recording where each page starts.

    Pages are joined with a space, or with nothing when the previous page ends in a
    hyphen, matching rule C4 for compounds split across a page break.
    """
    parts: list[str] = []
    starts: list[int] = []
    offset = 0
    for page in pages:
        if parts:
            separator = "" if parts[-1].endswith("-") else " "
            parts.append(separator)
            offset += len(separator)
        starts.append(offset)
        parts.append(page.text)
        offset += len(page.text)
    return Document("".join(parts), starts, [page.number for page in pages])


_SEGMENTER = pysbd.Segmenter(language="en", clean=False, char_span=True)


def split_sentences(text: str) -> list[Sentence]:
    """Sentence spans as character offsets into text, with surrounding whitespace excluded."""
    sentences = []
    for span in _SEGMENTER.segment(text):
        start, end = int(span.start), int(span.end)
        segment = text[start:end]
        start += len(segment) - len(segment.lstrip())
        end -= len(segment) - len(segment.rstrip())
        if start < end:
            sentences.append(Sentence(start, end))
    return sentences


def limit_length(
    text: str, sentences: Sequence[Sentence], config: ChunkingConfig
) -> list[Sentence]:
    """Split sentences longer than config.max_chars into pieces (rule S4).

    pysbd can return very long "sentences": run-on text from the lost-spacing paper, long
    equations or table rows, or an unpaired quote. Each is cut at the last space within
    max_chars, or at exactly max_chars when there is no space. Shorter sentences pass
    through unchanged. check_sentences() enforces the result.
    """
    results: list[Sentence] = []
    for sentence in sentences:
        if sentence.length <= config.max_chars:
            results.append(sentence)
            continue
        start = sentence.start
        while sentence.end - start > config.max_chars:
            limit = start + config.max_chars
            cut = text.rfind(" ", start + 1, limit + 1)
            if cut == -1:
                results.append(Sentence(start, limit))
                start = limit
            else:
                results.append(Sentence(start, cut))
                start = cut + 1
                while start < sentence.end and text[start].isspace():
                    start += 1
        if start < sentence.end:
            results.append(Sentence(start, sentence.end))
    return results


def pack(sentences: Sequence[Sentence], config: ChunkingConfig) -> list[range]:
    """Group sentences into chunks, returning one range of sentence indices per chunk.

    Greedy packing (S1, S2): a chunk takes sentences while its span stays within
    target_chars, and always takes at least one new sentence. Overlap (S3): each chunk
    starts with the previous chunk's last overlap_sentences sentences, reduced so the
    next chunk always moves forward, and dropped when overlap plus the new sentence would
    exceed max_chars. check_ranges() enforces the contract.
    """
    ranges: list[range] = []
    start = new_start = 0
    while new_start < len(sentences):
        stop = new_start + 1
        if sentences[stop - 1].end - sentences[start].start > config.max_chars:
            start = new_start
        while (
            stop < len(sentences)
            and sentences[stop].end - sentences[start].start <= config.target_chars
        ):
            stop += 1
        ranges.append(range(start, stop))
        overlap = min(config.overlap_sentences, stop - start - 1)
        new_start, start = stop, stop - overlap
    return ranges


def check_sentences(text: str, sentences: Sequence[Sentence], config: ChunkingConfig) -> None:
    """Enforce the limit_length() contract."""
    previous_end = 0
    for sentence in sentences:
        if not previous_end <= sentence.start < sentence.end <= len(text):
            raise ChunkingError(f"sentence {sentence} is out of order or out of bounds")
        if sentence.length > config.max_chars:
            raise ChunkingError(f"sentence {sentence} exceeds max_chars={config.max_chars}")
        previous_end = sentence.end


def check_ranges(
    sentences: Sequence[Sentence], ranges: Sequence[range], config: ChunkingConfig
) -> None:
    """Enforce the pack() contract."""
    if not sentences:
        if ranges:
            raise ChunkingError("ranges given for zero sentences")
        return
    if not ranges or ranges[0].start != 0 or ranges[-1].stop != len(sentences):
        raise ChunkingError("ranges must start at the first sentence and end at the last")
    for i, r in enumerate(ranges):
        if r.step != 1 or not 0 <= r.start < r.stop <= len(sentences):
            raise ChunkingError(f"range {i} ({r}) is empty, stepped or out of bounds")
        if i > 0:
            previous = ranges[i - 1]
            if r.start <= previous.start:
                raise ChunkingError(f"range {i} does not move forward")
            if r.start > previous.stop:
                raise ChunkingError(f"gap between ranges {i - 1} and {i}")
        length = sentences[r.stop - 1].end - sentences[r.start].start
        if length > config.max_chars:
            raise ChunkingError(f"chunk {i} is {length} chars, over max_chars={config.max_chars}")


def build_chunks(
    document: Document, sentences: Sequence[Sentence], ranges: Sequence[range]
) -> list[Chunk]:
    """Turn sentence ranges into chunks, with text sliced from the document and page ranges."""
    chunks = []
    for index, r in enumerate(ranges):
        start, end = sentences[r.start].start, sentences[r.stop - 1].end
        chunks.append(
            Chunk(
                index,
                document.text[start:end],
                document.page_at(start),
                document.page_at(end - 1),
            )
        )
    return chunks


def chunk(pages: Sequence[CleanPage], config: ChunkingConfig) -> list[Chunk]:
    """Chunk a cleaned document. A document with no pages yields no chunks."""
    if not pages:
        return []
    document = join_pages(pages)
    sentences = limit_length(document.text, split_sentences(document.text), config)
    check_sentences(document.text, sentences, config)
    ranges = pack(sentences, config)
    check_ranges(sentences, ranges, config)
    return build_chunks(document, sentences, ranges)
