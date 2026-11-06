"""The only place that knows which LLM provider is active.

Every agent calls `get_chat_model("fast" | "smart")`. Embeddings always use Google.
"""

from functools import lru_cache
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from app.config import settings

ModelTier = Literal["fast", "smart"]
REQUEST_TIMEOUT_SECONDS = 90


@lru_cache
def get_rate_limiter() -> InMemoryRateLimiter:
    """One shared limiter, so all agents together stay under the provider's per-minute limit."""
    return InMemoryRateLimiter(
        requests_per_second=settings.llm_requests_per_minute / 60,
        check_every_n_seconds=0.2,
        max_bucket_size=2,
    )


def model_for(tier: ModelTier) -> str:
    return settings.llm_fast_model if tier == "fast" else settings.llm_smart_model


def get_chat_model(tier: ModelTier = "fast") -> BaseChatModel:
    """Return the chat model for the configured provider.

    'fast' is for triage, checks and scheduling; 'smart' is for drafting and chat.
    Retries are done by `call_with_retry`, so the client libraries do not retry themselves.
    """
    if settings.llm_provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_for(tier),
            temperature=settings.llm_temperature,
            google_api_key=settings.google_api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
            rate_limiter=get_rate_limiter(),
        )

    if settings.llm_provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(
            model_name=model_for(tier),
            temperature=settings.llm_temperature,
            api_key=settings.groq_api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
            rate_limiter=get_rate_limiter(),
        )

    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")


@lru_cache
def get_embeddings() -> GoogleGenerativeAIEmbeddings:
    """Embeddings always use Google (Groq has no embeddings endpoint)."""
    return GoogleGenerativeAIEmbeddings(
        model=settings.embedding_model,
        google_api_key=settings.google_api_key,
        output_dimensionality=settings.embedding_dim,
    )
