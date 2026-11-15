"""Central configuration. Every path is relative to the project root; override with env vars (prefix CRM_) or `.env`."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Knowledge workspaces (document RBAC): product help docs are for everyone, the sales playbook for sales roles only.
DOCS_WS = "product-docs"
PLAYBOOK_WS = "sales-playbook"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), env_prefix="CRM_", extra="ignore")

    data_dir: Path = PROJECT_ROOT / "data" / "runtime"

    # Providers: "ollama" (default, local) or "fake" (deterministic, used by unit tests)
    llm_provider: str = "ollama"
    embed_provider: str = "ollama"
    rerank_provider: str = "cross-encoder"  # cross-encoder | overlap (tests)
    rerank_device: str = "auto"  # auto | cuda | cpu

    ollama_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    embed_model: str = "nomic-embed-text"
    reranker_model: str = "BAAI/bge-reranker-base"
    llm_num_ctx: int = 8192
    llm_temperature: float = 0.1

    # Retrieval
    retrieval_mode: str = "hybrid"  # bm25 | vector | hybrid
    use_reranker: bool = True
    candidates: int = 20
    top_k: int = 5
    rrf_k: int = 60
    # Only obviously unrelated questions are refused on the reranker score alone; the LLM's own "not found" check
    # handles the rest (measured: an answerable pricing question scored 0.009, an unanswerable one 0.049).
    not_found_threshold: float = 0.003

    # Chunking
    chunk_chars: int = 1100
    chunk_overlap_chars: int = 150

    # Assistant
    history_turns: int = 6  # previous messages given to the router / rewriter
    validate_answers: bool = True  # numeric/entity grounding check + one regeneration
    crm_seed: int = 7  # deterministic demo CRM data

    cors_origins: str = "http://localhost:3300,http://127.0.0.1:3300"
    release: str = "v1.0.0"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "crm.db"

    @property
    def qdrant_path(self) -> Path:
        return self.data_dir / "qdrant"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.upload_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
