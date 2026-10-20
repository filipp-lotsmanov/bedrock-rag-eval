import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_SPEC = importlib.util.spec_from_file_location(
    "judge_sensitivity", ROOT / "scripts" / "judge_sensitivity.py"
)
assert _SPEC is not None and _SPEC.loader is not None
js = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(js)

PROBE_RUN = ROOT / "results" / "20261019T194300Z_156a0cee6ec0"


def claim(verdict: str) -> dict[str, Any]:
    return {"claim": f"a {verdict} claim", "cited": [1], "verdict": verdict, "reason": "r"}


class FakeJudge:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls = 0

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        text = json.dumps({"claims": [claim(self.verdict)]})
        return {
            "output": {"message": {"content": [{"text": text}]}},
            "usage": {"inputTokens": 50, "outputTokens": 20},
        }


def results(text: str = "X rose by 10% [1].", abstained: bool = False) -> dict[str, dict[str, Any]]:
    result = {
        "question": "Q?",
        "text": text,
        "abstained": abstained,
        "passages": [{"text": "X rose by 10%."}],
    }
    return {"q1": {"id": "q1", "result": result}}


def seeded(
    answer_text: str = "X rose by 20% [1].", old: str = "10%", new: str = "20%"
) -> dict[str, Any]:
    return {
        "id": "n1",
        "source_qid": "q1",
        "type": "number",
        "edit": {"from": old, "to": new},
        "answer_text": answer_text,
    }


def control(answer_text: str = "By 10%, X rose [1].") -> dict[str, Any]:
    return {
        "id": "k1",
        "source_qid": "q1",
        "type": "control",
        "edit": None,
        "answer_text": answer_text,
    }


def test_validate_accepts_a_correct_seeded_probe_and_control() -> None:
    assert js.validate([seeded(), control()], results()) == []


@pytest.mark.parametrize(
    ("probe", "problem"),
    [
        (seeded(old="15%"), "occurs 0 times"),
        (seeded(answer_text="X fell by 20% [1]."), "exactly the stated edit"),
        (control("X rose by 10% [1]."), "identical"),
        (control("By 10%, X rose [2]."), "citations"),
    ],
)
def test_validate_rejects_probes_that_test_something_else(
    probe: dict[str, Any], problem: str
) -> None:
    problems = js.validate([probe], results())
    assert len(problems) == 1 and problem in problems[0]


def test_validate_rejects_an_abstained_source() -> None:
    assert "abstained" in js.validate([seeded()], results(abstained=True))[0]


@pytest.mark.parametrize(
    ("probe", "verdicts", "expected"),
    [
        (seeded(), ["supported", "partial"], True),
        (seeded(), ["supported", "supported"], False),
        (seeded(), [], False),
        (control(), ["supported"], True),
        (control(), ["supported", "unsupported"], False),
        (control(), [], False),
    ],
)
def test_passed(probe: dict[str, Any], verdicts: list[str], expected: bool) -> None:
    assert js.passed(probe, [claim(v) for v in verdicts]) is expected


def test_run_probes_resumes_and_rejudges_changed_text(tmp_path: Path) -> None:
    probes = [seeded(), control()]
    judge = FakeJudge("supported")
    js.run_probes(tmp_path, probes, results(), interval=0.0, client=judge)
    js.run_probes(tmp_path, probes, results(), interval=0.0, client=judge)
    assert judge.calls == 2

    probes[0] = seeded(answer_text="X rose by 30% [1].", new="30%")
    js.run_probes(tmp_path, probes, results(), interval=0.0, client=judge)
    assert judge.calls == 3
    judged = js.load_sensitivity(js.sensitivity_path(tmp_path), probes)
    assert set(judged) == {"n1", "k1"} and judged["n1"]["answer_sha"] == js.answer_sha(
        "X rose by 30% [1]."
    )


def test_judge_sees_the_probe_text_with_the_source_passages(tmp_path: Path) -> None:
    sent: list[str] = []

    class Recorder(FakeJudge):
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            sent.append(kwargs["messages"][0]["content"][0]["text"])
            return super().converse(**kwargs)

    js.run_probes(tmp_path, [seeded()], results(), interval=0.0, client=Recorder("partial"))
    assert "[1] X rose by 10%." in sent[0] and sent[0].endswith("Answer: X rose by 20% [1].")


def test_summarize_counts_types_and_lists_misses() -> None:
    probes = [seeded(), {**seeded(), "id": "n2"}, control()]
    judged = {
        "n1": {"claims": [claim("unsupported")]},
        "n2": {"claims": [claim("supported")]},
        "k1": {"claims": [claim("supported")]},
    }
    summary = js.summarize(probes, judged)
    assert summary["seeded_caught"] == {"numerator": 1, "denominator": 2}
    assert summary["controls_passed"] == {"passed": 1, "total": 1}
    assert [m["id"] for m in summary["misses"]] == ["n2"]


@pytest.mark.skipif(not (PROBE_RUN / "raw.jsonl").exists(), reason="probe source run not present")
def test_probe_file_matches_its_run() -> None:
    probes = js.load_probes(ROOT / "eval" / "judge_probes.yaml")
    assert js.validate(probes, js.latest_results(PROBE_RUN / "raw.jsonl")) == []
    assert len({p["id"] for p in probes}) == len(probes)
