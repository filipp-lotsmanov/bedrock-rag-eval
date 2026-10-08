import difflib
import math
import random

import pytest

from bakeoff import normalise, order_score, recovery, split_sentences, unwrap
from bakeoff.fidelity import _best_match


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hippo-\ncampus", "hippocampus"),
        ("hippo\u00ad\ncampus", "hippocampus"),
        ("neuro\u2010\nnal", "neuronal"),
        ("10-\n20", "10-20"),
        ("a - b - c", "a-b-c"),
        ("( Smith et al., 2021 ; Doe, 2020 ).", "(smith et al., 2021; doe, 2020)."),
    ],
)
def test_unwrap_then_normalise(raw: str, expected: str) -> None:
    assert normalise(unwrap(raw)) == expected


def test_xml_and_pdf_hyphen_forms_normalise_equal() -> None:
    assert normalise("amyloid- \u03b2") == normalise("amyloid-\u03b2")


def test_wrapped_pdf_sentence_is_one_sentence() -> None:
    pdf = "We measured calcium flux across three\nexperimental conditions in mice."
    assert split_sentences(pdf) == [
        "we measured calcium flux across three experimental conditions in mice."
    ]


def test_unpaired_quote_does_not_merge_sentences() -> None:
    body = " ".join(f"This is sentence number {i} about zither practice." for i in range(6))
    text = f"Learners lack \u201cbodily tools. {body} Later \u201ca quote\u201d ends it."
    sentences = split_sentences(text)
    assert len(sentences) == 8
    assert max(len(s) for s in sentences) < 60


def test_double_quotes_removed_apostrophes_kept() -> None:
    assert normalise(unwrap("\u201cbodily tools\u201d of Alzheimer\u2019s")) == (
        "bodily tools of alzheimer's"
    )


def test_short_fragments_are_dropped() -> None:
    assert split_sentences("Methods. Fig. 2.") == []


@pytest.mark.parametrize("length", [85, 120])
def test_length_blocking_keeps_reachable_candidates(length: int) -> None:
    needle = "abcdefghij" * 10
    candidate = needle[:length] if length < 100 else needle + "k" * (length - 100)
    ratio = difflib.SequenceMatcher(None, candidate, needle).ratio()
    assert ratio >= 0.90
    assert _best_match(needle, [candidate], 0.90) == 0


def test_pruned_search_agrees_with_brute_force() -> None:
    rng = random.Random(0)
    alphabet = "abcde "

    def text(size: int) -> str:
        return "".join(rng.choice(alphabet) for _ in range(size))

    for _ in range(500):
        needle = text(rng.randint(20, 60))
        haystack = [needle[: rng.randint(10, 60)] + text(rng.randint(0, 15)) for _ in range(5)]
        for threshold in (0.90, 0.95, 0.99):
            scores = [
                difflib.SequenceMatcher(None, h, needle, autojunk=False).ratio() for h in haystack
            ]
            reachable = [i for i, s in enumerate(scores) if s >= threshold]
            expected = min(reachable, key=lambda i: (-scores[i], i)) if reachable else None
            assert _best_match(needle, haystack, threshold) == expected


def test_ties_resolve_to_first_occurrence() -> None:
    needle = "the same running header text here"
    near = "the same running header text herx"
    assert _best_match(needle, [near, near], 0.90) == 0


TRUTH = [
    "sigma-1 receptors are enriched at mitochondria-associated er membranes.",
    "we measured calcium flux across three experimental conditions in mice.",
    "the results indicate a dose-dependent response to the ligand tested.",
    "no significant difference was observed in the control group at baseline.",
]


def test_recovery_and_order_identity() -> None:
    fraction, matched = recovery(TRUTH, TRUTH, 0.95)
    assert fraction == 1.0
    assert order_score(matched) == pytest.approx(1.0)


def test_reversed_order_scores_minus_one() -> None:
    _, matched = recovery(TRUTH, TRUTH[::-1], 0.95)
    assert order_score(matched) == pytest.approx(-1.0)


def test_empty_truth_and_small_samples() -> None:
    assert recovery([], TRUTH, 0.95) == (0.0, [])
    assert math.isnan(order_score([3]))