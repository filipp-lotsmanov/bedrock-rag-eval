"""Retrieval: vector search over the Knowledge Base, optionally reranked, in one Retrieve call."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from query.config import QueryConfig

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agent_runtime import AgentsforBedrockRuntimeClient


@dataclass(frozen=True)
class Passage:
    text: str
    doc_id: str
    doi: str
    page_start: int
    page_end: int
    chunk_index: int
    score: float
    source_uri: str


def retrieval_configuration(config: QueryConfig) -> dict[str, Any]:
    """Without a reranker, fetch top_k directly; with one, fetch candidates and rerank to top_k."""
    if config.rerank_model_arn is None:
        return {"vectorSearchConfiguration": {"numberOfResults": config.top_k}}
    return {
        "vectorSearchConfiguration": {
            "numberOfResults": config.candidates,
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": config.rerank_model_arn},
                    "numberOfRerankedResults": config.top_k,
                },
            },
        }
    }


def to_passage(result: dict[str, Any]) -> Passage:
    """Numeric metadata comes back as floats, so page and chunk numbers are cast to int."""
    metadata = result["metadata"]
    return Passage(
        text=result["content"]["text"],
        doc_id=str(metadata["doc_id"]),
        doi=str(metadata["doi"]),
        page_start=int(metadata["page_start"]),
        page_end=int(metadata["page_end"]),
        chunk_index=int(metadata["chunk_index"]),
        score=float(result["score"]),
        source_uri=result["location"]["s3Location"]["uri"],
    )


def retrieve(
    client: "AgentsforBedrockRuntimeClient",
    knowledge_base_id: str,
    question: str,
    config: QueryConfig,
) -> list[Passage]:
    response = client.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": question},
        retrievalConfiguration=retrieval_configuration(config),  # type: ignore[arg-type]
    )
    passages = [to_passage(dict(result)) for result in response["retrievalResults"]]
    return sorted(passages, key=lambda p: p.score, reverse=True)[: config.top_k]
