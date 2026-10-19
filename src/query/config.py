"""Query strategy settings. Every value here is recorded with each answer via config_id."""

import hashlib
import json
from dataclasses import asdict, dataclass

AMAZON_RERANK = "arn:aws:bedrock:eu-central-1::foundation-model/amazon.rerank-v1:0"
NOVA_LITE = "eu.amazon.nova-lite-v1:0"
MAX_RESULTS = 100


@dataclass(frozen=True)
class QueryConfig:
    generation_model_id: str = NOVA_LITE
    rerank_model_arn: str | None = AMAZON_RERANK
    candidates: int = 20
    top_k: int = 5
    max_tokens: int = 512
    temperature: float = 0.0
    abstain_below: float | None = None

    def __post_init__(self) -> None:
        if not 0 < self.top_k <= self.candidates <= MAX_RESULTS:
            raise ValueError(f"need 0 < top_k <= candidates <= {MAX_RESULTS}")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

    @property
    def config_id(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()[:12]


DEFAULT_QUERY_CONFIG = QueryConfig()
