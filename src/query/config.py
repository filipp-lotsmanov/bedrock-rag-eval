"""Query strategy settings. Every value here is recorded with each answer via config_id."""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

# The region and model IDs, shared with Terraform (infra/providers.tf reads the same file),
# so the code and the deployed infrastructure cannot disagree on them.
MODELS = json.loads((Path(__file__).parent / "models.json").read_text(encoding="utf-8"))
AMAZON_RERANK = f"arn:aws:bedrock:{MODELS['region']}::foundation-model/{MODELS['rerank_model_id']}"
NOVA_LITE: str = MODELS["generation_model_id"]
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
