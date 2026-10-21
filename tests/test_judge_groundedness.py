import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

_SPEC = importlib.util.spec_from_file_location(
    "judge_groundedness", Path(__file__).resolve().parents[1] / "scripts" / "judge_groundedness.py"
)
assert _SPEC is not None and _SPEC.loader is not None
jg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(jg)


def claim(verdict: str, cited: list[int] | None = None) -> dict[str, Any]:
    return {"claim": f"a {verdict} claim", "cited": cited or [1], "verdict": verdict, "reason": "r"}


def reply(claims: list[dict[str, Any]]) -> str:
    return json.dumps({"claims": claims})


class FakeJudge:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        text = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return {
            "output": {"message": {"content": [{"text": text}]}},
            "usage": {"inputTokens": 50, "outputTokens": 20},
        }


def test_parse_claims_accepts_a_code_fence() -> None:
    claims = jg.parse_claims("```json\n" + reply([claim("supported", [2])]) + "\n```")
    assert claims[0]["verdict"] == "supported" and claims[0]["cited"] == [2]


@pytest.mark.parametrize(
    "text",
    [
        "no json here",
        '{"other": []}',
        reply([claim("maybe")]),
        '{"claims": ["a bare string"]}',
        '{"claims": [{"claim": "c", "verdict": "supported", "cited": null}]}',
        '{"claims": [{"claim": "c", "verdict": "supported", "cited": ["one"]}]}',
        '{"claims": [{"claim": "c", "verdict": "supported", "cited": [true]}]}',
    ],
)
def test_parse_claims_rejects_malformed_replies(text: str) -> None:
    with pytest.raises(ValueError):
        jg.parse_claims(text)


def test_judge_message_shows_only_cited_passages_under_their_numbers() -> None:
    passages = [{"text": "alpha"}, {"text": "beta"}, {"text": "gamma"}]
    result = {"passages": passages, "text": "Gamma [3], beta [2][3], none [9]."}
    message = jg.judge_message("Q?", result)
    assert "[2] beta\n\n[3] gamma" in message and "alpha" not in message
    assert message.endswith("Answer: Gamma [3], beta [2][3], none [9].")


def test_judge_message_says_so_when_nothing_is_cited() -> None:
    message = jg.judge_message("Q?", {"passages": [{"text": "alpha"}], "text": "Alpha."})
    assert "(the answer cites no passage)" in message and "alpha" not in message


def test_judge_id_covers_the_message_format(monkeypatch: pytest.MonkeyPatch) -> None:
    before = jg.prompt_id()
    monkeypatch.setattr(jg, "MESSAGE_FORMAT", "all-passages")
    assert jg.prompt_id() != before


@pytest.mark.parametrize(
    "bad", ["not json", '{"claims": ["a bare string"]}', '{"claims": [{"cited": null}]}']
)
def test_judge_one_retries_once_after_a_malformed_reply(bad: str) -> None:
    fake = FakeJudge([bad, reply([claim("supported")])])
    claims, _, usage = jg.judge_one(fake, "message")
    assert fake.calls == 2 and claims[0]["verdict"] == "supported" and usage["output_tokens"] == 20


def test_summary_counts_claims_and_fully_grounded_answers() -> None:
    judgements = {
        "q1": {"id": "q1", "claims": [claim("supported"), claim("supported")]},
        "q2": {"id": "q2", "claims": [claim("supported"), claim("partial")]},
        "q3": {"id": "q3", "claims": []},
    }
    summary = jg.summarize(judgements)
    assert summary["verdicts"] == {"supported": 3, "partial": 1, "unsupported": 0}
    assert summary["claim_support_rate"] == 0.75
    assert summary["fully_grounded_answers"] == {"numerator": 1, "denominator": 2}
    assert [item["id"] for item in summary["not_fully_supported"]] == ["q2"]


def test_agreement_exact_and_binary() -> None:
    judgements = {"q1": {"claims": [claim("supported"), claim("partial"), claim("unsupported")]}}
    result = jg.agreement(judgements, {"q1": ["supported", "unsupported", "unsupported"]})
    assert result["exact_agreement"] == 0.667
    assert result["supported_vs_not_agreement"] == 1.0


def write_run(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    base = {"passages": [{"text": "p"}], "text": "x [1]", "question": "Q"}
    records = [
        {"id": "q1", "result": {**base, "abstained": False}},
        {"id": "q2", "result": {**base, "abstained": False}},
        {"id": "u1", "result": {**base, "abstained": True, "text": None}},
    ]
    (run_dir / "raw.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    questions = tmp_path / "questions.yaml"
    entries = [{"id": i, "question": "Q", "review": "accepted"} for i in ("q1", "q2", "u1")]
    questions.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return run_dir, questions


def test_only_answered_replies_are_judged_and_errors_are_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, questions = write_run(tmp_path)
    items = jg.answered(run_dir, questions)
    assert [q["id"] for q, _ in items] == ["q1", "q2"]

    first = FakeJudge([reply([claim("supported")]), "bad", "bad"])
    monkeypatch.setattr(jg.boto3, "client", lambda *a, **k: first)
    jg.run_judge(run_dir, items, interval=0)
    assert set(jg.load_judgements(jg.judge_path(run_dir))) == {"q1"}

    second = FakeJudge([reply([claim("partial")])])
    monkeypatch.setattr(jg.boto3, "client", lambda *a, **k: second)
    jg.run_judge(run_dir, items, interval=0)
    assert second.calls == 1
    assert jg.load_judgements(jg.judge_path(run_dir))["q2"]["claims"][0]["verdict"] == "partial"
