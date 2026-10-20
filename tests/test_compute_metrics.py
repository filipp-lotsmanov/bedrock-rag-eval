import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

_SPEC = importlib.util.spec_from_file_location(
    "compute_metrics", Path(__file__).resolve().parents[1] / "scripts" / "compute_metrics.py"
)
assert _SPEC is not None and _SPEC.loader is not None
cm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cm)


def passage(doc: str, start: int, end: int | None = None) -> dict[str, Any]:
    return {"doc_id": doc, "page_start": start, "page_end": end or start, "score": 0.5}


def question(qid: str, category: str, docs: list[str], pages: list[int]) -> dict[str, Any]:
    return {
        "id": qid,
        "category": category,
        "expected_docs": docs,
        "expected_pages": pages,
        "review": "accepted",
    }


def result(
    passages: list[dict[str, Any]],
    abstained: bool = False,
    citations: list[dict[str, Any]] | None = None,
    invalid: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "abstained": abstained,
        "passages": passages,
        "citations": citations or [],
        "invalid_citations": invalid or [],
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "text": None if abstained else "x [1]",
    }


def test_page_ranges_overlap_expected_pages() -> None:
    assert cm.overlaps(5, 6, [6])
    assert not cm.overlaps(5, 6, [7])


def test_hit_at_rank_two_gives_reciprocal_rank_half() -> None:
    q = question("q1", "answerable", ["A"], [6])
    row = cm.per_question(
        q, result([passage("A", 2), passage("A", 5, 6)], citations=[passage("A", 6)]), None
    )
    assert (row["doc_hit"], row["page_hit"], row["reciprocal_rank"]) == (True, True, 0.5)
    assert row["citation_on_evidence"] and row["citation_valid"]


def test_right_paper_wrong_page_is_a_doc_hit_only() -> None:
    q = question("q1", "answerable", ["A"], [6])
    row = cm.per_question(
        q, result([passage("A", 2), passage("B", 6)], citations=[passage("A", 2)]), None
    )
    assert (row["doc_hit"], row["page_hit"], row["reciprocal_rank"]) == (True, False, 0.0)
    assert not row["citation_on_evidence"]


def test_abstained_answerable_has_no_citation_metrics() -> None:
    row = cm.per_question(
        question("q1", "answerable", ["A"], [1]), result([passage("A", 1)], abstained=True), None
    )
    assert "citation_on_evidence" not in row and "citation_valid" not in row


@pytest.mark.parametrize(
    ("abstained", "grade", "expected"),
    [(True, None, True), (False, None, None), (False, False, False)],
)
def test_false_premise_counts_abstention_or_grade(
    abstained: bool, grade: bool | None, expected: bool | None
) -> None:
    row = cm.per_question(
        question("u1", "false_premise", ["A"], [1]), result([], abstained=abstained), grade
    )
    assert row["premise_correct"] is expected


def test_summary_rates_and_denominators() -> None:
    rows = [
        cm.per_question(
            question("q1", "answerable", ["A"], [1]),
            result([passage("A", 1)], citations=[passage("A", 1)]),
            None,
        ),
        cm.per_question(
            question("q2", "answerable", ["A"], [1]), result([passage("B", 1)], invalid=[9]), None
        ),
        cm.per_question(question("u1", "not_covered", [], []), result([], abstained=True), None),
        cm.per_question(question("u2", "off_topic", [], []), result([]), None),
        cm.per_question(question("u3", "false_premise", ["A"], [1]), result([]), None),
    ]
    summary = cm.summarize(rows, 5)
    assert summary["page_hit@k"] == {"value": 0.5, "numerator": 1, "denominator": 2}
    assert summary["mrr"]["value"] == 0.5
    assert (
        summary["correct_abstention"]["numerator"] == 1
        and summary["correct_abstention"]["denominator"] == 2
    )
    assert summary["citation_validity"] == {"value": 0.75, "numerator": 3, "denominator": 4}
    assert summary["premise_handling"] == {
        "value": None,
        "numerator": 0,
        "denominator": 0,
        "ungraded": 1,
    }


def test_latest_successful_result_wins(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    lines = [
        {"id": "q1", "error": "boom"},
        {"id": "q1", "result": {"n": 1}},
        {"id": "q1", "error": "late"},
    ]
    raw.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    assert cm.latest_results(raw)["q1"]["result"] == {"n": 1}


def make_run(tmp_path: Path, questions_sha: str) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run = {
        "run_id": "r1",
        "config_id": "c",
        "git": {},
        "config": {"top_k": 5},
        "questions_sha256": questions_sha,
    }
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    record = {
        "id": "q1",
        "latency_s": 1.0,
        "result": result([passage("A", 1)], citations=[passage("A", 1)]),
    }
    (run_dir / "raw.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    return run_dir


def test_compute_writes_metrics_and_guards_labels(tmp_path: Path) -> None:
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        yaml.safe_dump([question("q1", "answerable", ["A"], [1])]), encoding="utf-8"
    )
    good = make_run(tmp_path, hashlib.sha256(questions.read_bytes()).hexdigest())
    metrics = cm.compute(good, questions, allow_label_change=False, grade=False)
    assert metrics["summary"]["page_hit@k"]["value"] == 1.0
    assert metrics["labels_match_run"] is True
    assert (good / "metrics.json").exists()
    questions.write_text(questions.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="changed since run"):
        cm.compute(good, questions, allow_label_change=False, grade=False)
    relabeled = cm.compute(good, questions, allow_label_change=True, grade=False)
    assert relabeled["labels_match_run"] is False
