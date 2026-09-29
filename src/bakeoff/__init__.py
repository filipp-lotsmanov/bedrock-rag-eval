"""Parser bake-off: ground-truth extraction and fidelity metrics."""

from bakeoff.fidelity import (
    MIN_SENTENCE_CHARS,
    normalise,
    order_score,
    recovery,
    split_sentences,
    unwrap,
)
from bakeoff.jats import extract_ground_truth

__all__ = [
    "MIN_SENTENCE_CHARS",
    "extract_ground_truth",
    "normalise",
    "order_score",
    "recovery",
    "split_sentences",
    "unwrap",
]