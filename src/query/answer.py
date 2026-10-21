"""The query path: retrieve, gate, generate, cite. Shared by the API handler and the eval runner.

Abstention happens in two places:
    - before generation, when nothing is retrieved or the top score is below
      config.abstain_below (off until the threshold is calibrated on the eval set)
    - after generation, when the reply is empty, is prompt.REFUSAL, or cites no retrieved
      passage, so every answer returned has at least one valid citation

A reply cut off at config.max_tokens is still returned; stop_reason records it.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from query import prompt
from query.config import QueryConfig
from query.generate import Generation, generate, parse_citations
from query.retrieve import Passage, retrieve

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agent_runtime import AgentsforBedrockRuntimeClient
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

MAX_QUESTION_CHARS = 500


class BadRequest(ValueError):
    """The question or request body is invalid; the API answers it with HTTP 400."""


@dataclass(frozen=True)
class Citation:
    number: int
    doc_id: str
    doi: str
    page_start: int
    page_end: int


@dataclass(frozen=True)
class Answer:
    question: str
    text: str | None
    abstained: bool
    abstain_reason: str | None
    citations: list[Citation]
    invalid_citations: list[int]
    passages: list[Passage]
    config_id: str
    generation_model_id: str
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Everything, for eval runs and logs."""
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """What the API returns: the answer and its cited passages, without retrieval internals
        (uncited passages, S3 locations, abstain reasons, token usage)."""
        return {
            "question": self.question,
            "text": self.text,
            "abstained": self.abstained,
            "citations": [
                asdict(c)
                | {
                    "text": self.passages[c.number - 1].text,
                    "score": self.passages[c.number - 1].score,
                }
                for c in self.citations
            ],
            "config_id": self.config_id,
            "generation_model_id": self.generation_model_id,
        }


def validate_question(question: str) -> str:
    question = question.strip()
    if not question:
        raise BadRequest("question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise BadRequest(f"question is longer than {MAX_QUESTION_CHARS} characters")
    return question


_CITATION_MARKER = re.compile(r"\[\d+\]")


def is_refusal(text: str) -> bool:
    """The bare refusal token. Citation markers and trailing punctuation around it are ignored,
    since Nova Lite sometimes cites the passages it found insufficient; the token inside a
    longer reply is not a refusal, because that reply also makes claims."""
    remainder = _CITATION_MARKER.sub("", text)
    return remainder.strip().rstrip(".!").strip() == prompt.REFUSAL


def answer(
    question: str,
    knowledge_base_id: str,
    config: QueryConfig,
    agent_runtime: "AgentsforBedrockRuntimeClient",
    bedrock_runtime: "BedrockRuntimeClient",
    system_prompt: str | None = None,
) -> Answer:
    system_prompt = system_prompt or prompt.SYSTEM_PROMPT
    if not system_prompt:
        raise RuntimeError("system prompt is empty")
    question = validate_question(question)
    passages = retrieve(agent_runtime, knowledge_base_id, question, config)

    def abstain(reason: str, generation: Generation | None = None) -> Answer:
        usage = (
            {}
            if generation is None
            else {
                "input_tokens": generation.input_tokens,
                "output_tokens": generation.output_tokens,
            }
        )
        return Answer(
            question,
            None,
            True,
            reason,
            [],
            [],
            passages,
            config.config_id,
            config.generation_model_id,
            usage,
            None if generation is None else generation.stop_reason,
        )

    if not passages:
        return abstain("no passages retrieved")
    if config.abstain_below is not None and passages[0].score < config.abstain_below:
        return abstain(f"top score {passages[0].score:.3f} below threshold {config.abstain_below}")

    generation = generate(
        bedrock_runtime, system_prompt, prompt.user_message(question, passages), config
    )
    if not generation.text:
        return abstain(f"empty reply (stop_reason={generation.stop_reason})", generation)
    if is_refusal(generation.text):
        return abstain("model found the passages insufficient", generation)

    valid, invalid = parse_citations(generation.text, len(passages))
    if not valid:
        return abstain(f"reply cites no retrieved passage (invalid: {invalid})", generation)
    citations = [
        Citation(
            n,
            passages[n - 1].doc_id,
            passages[n - 1].doi,
            passages[n - 1].page_start,
            passages[n - 1].page_end,
        )
        for n in valid
    ]
    usage = {
        "input_tokens": generation.input_tokens,
        "output_tokens": generation.output_tokens,
    }
    return Answer(
        question,
        generation.text,
        False,
        None,
        citations,
        invalid,
        passages,
        config.config_id,
        config.generation_model_id,
        usage,
        generation.stop_reason,
    )
