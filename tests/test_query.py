import base64
import json
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError

from query import handler, prompt
from query.answer import MAX_QUESTION_CHARS, answer, is_refusal
from query.config import QueryConfig
from query.generate import parse_citations
from query.retrieve import retrieval_configuration, to_passage

SYSTEM = "Answer only from the passages and cite them."


def result(
    doc_id: str, score: float, text: str = "Some text.", page: float = 3.0
) -> dict[str, Any]:
    return {
        "content": {"text": text},
        "metadata": {
            "doc_id": doc_id,
            "doi": f"10.1/{doc_id}",
            "page_start": page,
            "page_end": page + 1,
            "chunk_index": 4.0,
        },
        "score": score,
        "location": {"s3Location": {"uri": f"s3://processed/chunks/{doc_id}/0004.txt"}},
    }


class FakeAgentRuntime:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"retrievalResults": self.results}


class FakeBedrockRuntime:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": self.text}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 900, "outputTokens": 60, "totalTokens": 960},
        }


def run(
    results: list[dict[str, Any]], reply: str, config: QueryConfig | None = None
) -> tuple[Any, FakeAgentRuntime, FakeBedrockRuntime]:
    agent, bedrock = FakeAgentRuntime(results), FakeBedrockRuntime(reply)
    out = answer(
        "What does DSV do?",
        "KB",
        config or QueryConfig(),
        cast(Any, agent),
        cast(Any, bedrock),
        SYSTEM,
    )
    return out, agent, bedrock


def test_config_validation_and_id() -> None:
    with pytest.raises(ValueError):
        QueryConfig(top_k=30, candidates=20)
    with pytest.raises(ValueError):
        QueryConfig(candidates=101)
    assert QueryConfig().config_id == QueryConfig().config_id
    assert QueryConfig().config_id != QueryConfig(rerank_model_arn=None).config_id


def test_retrieval_configuration_with_and_without_reranker() -> None:
    reranked = retrieval_configuration(QueryConfig())["vectorSearchConfiguration"]
    assert reranked["numberOfResults"] == 20
    assert (
        reranked["rerankingConfiguration"]["bedrockRerankingConfiguration"][
            "numberOfRerankedResults"
        ]
        == 5
    )
    plain = retrieval_configuration(QueryConfig(rerank_model_arn=None))["vectorSearchConfiguration"]
    assert plain == {"numberOfResults": 5}


def test_passage_casts_float_metadata_to_int() -> None:
    p = to_passage(result("PMC1", 0.8, page=3.0))
    assert (p.page_start, p.page_end, p.chunk_index) == (3, 4, 4)
    assert isinstance(p.page_start, int)


def test_parse_citations_orders_and_separates_invalid() -> None:
    assert parse_citations("A [2]. B [1][2]. C [7] and [0].", 5) == ([2, 1], [7, 0])


@pytest.mark.parametrize(
    "reply",
    [
        "INSUFFICIENT_CONTEXT",
        " INSUFFICIENT_CONTEXT. ",
        "INSUFFICIENT_CONTEXT!",
        "INSUFFICIENT_CONTEXT [1][5]",
        "INSUFFICIENT_CONTEXT [1][2][3][4][5].",
    ],
)
def test_refusal_variants(reply: str) -> None:
    assert is_refusal(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "The passages give INSUFFICIENT_CONTEXT for part of this [1].",
        "The control group did not outperform the training group [3]. INSUFFICIENT_CONTEXT.",
    ],
)
def test_refusal_inside_a_longer_reply_is_not_a_refusal(reply: str) -> None:
    assert not is_refusal(reply)


def test_answer_maps_citations_to_passages() -> None:
    out, _, bedrock = run(
        [result("PMC1", 0.9), result("PMC2", 0.7, page=8.0)],
        "DSV induces it [1], also [2][9].",
    )
    assert not out.abstained
    assert [(c.number, c.doc_id, c.page_start) for c in out.citations] == [
        (1, "PMC1", 3),
        (2, "PMC2", 8),
    ]
    assert out.invalid_citations == [9]
    assert out.usage == {"input_tokens": 900, "output_tokens": 60}
    sent = bedrock.calls[0]["messages"][0]["content"][0]["text"]
    assert "[1] (PMC1, pp. 3-4)" in sent and sent.endswith("Question: What does DSV do?")
    assert bedrock.calls[0]["system"] == [{"text": SYSTEM}]


def test_no_passages_abstains_without_calling_the_model() -> None:
    out, _, bedrock = run([], "unused")
    assert out.abstained and out.abstain_reason == "no passages retrieved"
    assert bedrock.calls == []


def test_low_top_score_abstains_without_calling_the_model() -> None:
    out, _, bedrock = run([result("PMC1", 0.2)], "unused", QueryConfig(abstain_below=0.5))
    assert out.abstained and "below threshold" in str(out.abstain_reason)
    assert bedrock.calls == []


def test_model_refusal_abstains_and_keeps_usage() -> None:
    out, _, _ = run([result("PMC1", 0.9)], prompt.REFUSAL)
    assert out.abstained and out.text is None and out.citations == []
    assert out.usage["output_tokens"] == 60


def test_passages_are_sorted_and_capped_at_top_k() -> None:
    results = [
        result(f"PMC{i}", score) for i, score in enumerate([0.1, 0.9, 0.5, 0.7, 0.3, 0.8, 0.6])
    ]
    out, _, _ = run(results, "x [1]")
    assert [p.score for p in out.passages] == [0.9, 0.8, 0.7, 0.6, 0.5]


@pytest.mark.parametrize("question", ["", "   ", "x" * (MAX_QUESTION_CHARS + 1)])
def test_invalid_questions_are_rejected(question: str) -> None:
    with pytest.raises(ValueError):
        answer(
            question,
            "KB",
            QueryConfig(),
            cast(Any, FakeAgentRuntime([])),
            cast(Any, FakeBedrockRuntime("")),
            SYSTEM,
        )


def test_missing_system_prompt_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompt, "SYSTEM_PROMPT", None)
    with pytest.raises(RuntimeError, match="system prompt"):
        answer(
            "q",
            "KB",
            QueryConfig(),
            cast(Any, FakeAgentRuntime([])),
            cast(Any, FakeBedrockRuntime("")),
        )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeBedrockRuntime:
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "KB")
    monkeypatch.setattr(prompt, "SYSTEM_PROMPT", SYSTEM)
    bedrock = FakeBedrockRuntime("DSV induces it [1].")
    monkeypatch.setattr(handler, "_agent_runtime", lambda: FakeAgentRuntime([result("PMC1", 0.9)]))
    monkeypatch.setattr(handler, "_bedrock_runtime", lambda: bedrock)
    return bedrock


def event(body: str, base64_encoded: bool = False) -> dict[str, Any]:
    return {
        "body": base64.b64encode(body.encode()).decode() if base64_encoded else body,
        "isBase64Encoded": base64_encoded,
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "user-123"}}}},
    }


@pytest.mark.usefixtures("api")
@pytest.mark.parametrize("encoded", [False, True])
def test_handler_answers(encoded: bool) -> None:
    response = handler.main(event(json.dumps({"question": "What does DSV do?"}), encoded), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["citations"][0]["doc_id"] == "PMC1" and body["abstained"] is False


@pytest.mark.usefixtures("api")
@pytest.mark.parametrize(
    "body", ["not json", "[]", '{"q": "x"}', json.dumps({"question": "x" * 600})]
)
def test_handler_rejects_bad_requests(body: str) -> None:
    response = handler.main(event(body), None)
    assert response["statusCode"] == 400
    assert "error" in json.loads(response["body"])


def test_handler_hides_internal_errors(
    api: FakeBedrockRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Any:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(handler, "_agent_runtime", broken)
    response = handler.main(event(json.dumps({"question": "q"})), None)
    assert response["statusCode"] == 500
    assert "secret" not in response["body"]


@pytest.mark.parametrize("code", ["ThrottlingException", "TooManyRequestsException"])
def test_handler_turns_throttling_into_429(
    api: FakeBedrockRuntime, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    class Throttled(FakeAgentRuntime):
        def retrieve(self, **kwargs: Any) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": code, "Message": "rate"}}, "Retrieve")

    monkeypatch.setattr(handler, "_agent_runtime", lambda: Throttled([]))
    response = handler.main(event(json.dumps({"question": "q"})), None)
    assert response["statusCode"] == 429
    assert response["headers"]["retry-after"] == "30"


def test_handler_other_client_errors_are_500(
    api: FakeBedrockRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Denied(FakeAgentRuntime):
        def retrieve(self, **kwargs: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "Retrieve",
            )

    monkeypatch.setattr(handler, "_agent_runtime", lambda: Denied([]))
    response = handler.main(event(json.dumps({"question": "q"})), None)
    assert response["statusCode"] == 500
    assert "AccessDenied" not in response["body"]
