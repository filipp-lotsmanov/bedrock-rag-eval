"""Generation: one Converse call, and parsing of the [n] citations in the answer."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from query.config import QueryConfig

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

_CITATION = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class Generation:
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int


def generate(
    client: "BedrockRuntimeClient",
    system_prompt: str,
    message: str,
    config: QueryConfig,
) -> Generation:
    response = client.converse(
        modelId=config.generation_model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": message}]}],
        inferenceConfig={
            "maxTokens": config.max_tokens,
            "temperature": config.temperature,
        },
    )
    content = response["output"].get("message", {}).get("content", [])
    return Generation(
        text="".join(block.get("text", "") for block in content).strip(),
        stop_reason=response["stopReason"],
        input_tokens=response["usage"]["inputTokens"],
        output_tokens=response["usage"]["outputTokens"],
    )


def parse_citations(text: str, passage_count: int) -> tuple[list[int], list[int]]:
    """(valid, invalid) passage numbers, each in order of first appearance, without repeats."""
    valid: list[int] = []
    invalid: list[int] = []
    for match in _CITATION.finditer(text):
        number = int(match.group(1))
        target = valid if 1 <= number <= passage_count else invalid
        if number not in target:
            target.append(number)
    return valid, invalid
