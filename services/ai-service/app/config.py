import re

from pydantic import SecretStr
from sfo_common import BaseServiceSettings
from sfo_common.config import KafkaConsumerSettings


class Settings(BaseServiceSettings, KafkaConsumerSettings):
    # Vector store (docs/week4-plan.md B4-D2). Short timeout: a Qdrant outage
    # fails a search or readiness check fast instead of hanging the request.
    qdrant_url: str = "http://qdrant:6333"
    qdrant_timeout_seconds: int = 5

    # Embeddings (B4-D3): fastembed, local ONNX models. The image downloads
    # them at build time into this directory (Dockerfile); empty means
    # fastembed's default cache, for running outside Docker.
    dense_model: str = "BAAI/bge-small-en-v1.5"
    dense_dim: int = 384
    # BGE's recommended instruction for short search queries (not for the
    # documents). Empty turns it off — step 5's retrieval eval compares both.
    dense_query_prefix: str = "Represent this sentence for searching relevant passages: "
    sparse_model: str = "Qdrant/bm25"
    # Weighted RRF (app/retrieval.py): how much each list counts in the
    # fused ranking. Equal weights make "1st by one, 2nd by the other" an
    # exact tie, ordered arbitrarily between runs. On the golden set
    # (scripts/eval_retrieval.py) weights and the query prefix moved scores by
    # at most one query; dense 1.2 ties for best and makes the order stable.
    fusion_dense_weight: float = 1.2
    fusion_sparse_weight: float = 1.0
    embedding_cache_dir: str = ""
    # ONNX threads per model. Embedding runs off the event loop
    # (app/embedding.py), one batch at a time.
    embedding_threads: int = 2
    # Bump when the documents' text or payload changes shape: with the dense
    # model it names the collections (B4-D6), so a new index is built next to
    # the old one by `python -m app.reindex` instead of mixing two formats.
    index_revision: int = 1

    # Where the backfill reads from (app/reindex.py) — internal endpoints.
    restaurant_service_url: str = "http://restaurant-service:8000"
    order_service_url: str = "http://order-service:8000"

    # Claude (Week 5). Empty until the user adds a key to .env; SecretStr keeps
    # it out of logs and reprs.
    anthropic_api_key: SecretStr = SecretStr("")

    @property
    def index_suffix(self) -> str:
        """`bge_small_en_v1_5_r1`: the dense model and document revision."""
        model = re.sub(r"[^a-z0-9]+", "_", self.dense_model.rsplit("/", 1)[-1].lower())
        return f"{model}_r{self.index_revision}"


settings = Settings()  # type: ignore[call-arg]
