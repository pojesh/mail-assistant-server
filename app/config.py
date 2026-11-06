"""Application settings, read from environment variables (and `.env` during local development)."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Settings that must be present in production. Startup fails fast if any is empty.
PRODUCTION_REQUIRED = (
    "session_secret",
    "token_encryption_key",
    "google_api_key",
    "database_url",
    "checkpoint_database_url",
    "google_oauth_client_id",
    "google_oauth_client_secret",
    "pubsub_topic",
    "internal_invoker_sa",
    "internal_audience",
)


class Settings(BaseSettings):
    """All configuration for the API service. Field names map to UPPER_CASE env vars."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: Literal["development", "production"] = "development"
    app_base_url: str = "http://localhost:3000"
    session_secret: str = ""
    token_encryption_key: str = ""
    cors_origins: str = "http://localhost:3000"
    max_emails_per_run: int = Field(10, ge=1, le=100)

    # --- LLM provider ---
    llm_provider: Literal["google", "groq"] = "google"
    llm_fast_model: str = "gemini-3.1-flash-lite"
    llm_smart_model: str = "gemini-3.1-flash-lite"
    llm_temperature: float = Field(0.2, ge=0, le=2)
    # NOTE: free tiers limit requests per minute (Gemini flash-lite: 15). All models in this
    # process share one limiter set slightly below the provider's limit.
    llm_requests_per_minute: int = Field(14, ge=1, le=1000)
    google_api_key: str = ""
    groq_api_key: str = ""

    # --- Embeddings (always Google) ---
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768

    # --- Database ---
    database_url: str = ""
    checkpoint_database_url: str = ""
    # NOTE: only set by the test suite, to run integration tests in a throwaway schema.
    database_schema: str = ""

    # --- Google OAuth / APIs ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_path: str = "/api/auth/google/callback"
    gcp_project_id: str = ""
    pubsub_topic: str = ""
    internal_invoker_sa: str = ""
    internal_audience: str = ""

    # --- Observability ---
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def check_required_settings(self) -> Self:
        """Reject configurations that would fail later at runtime."""
        if not self.google_api_key:
            raise ValueError("GOOGLE_API_KEY is required (it is also used for embeddings).")
        # NOTE: also required in development — sign-in and token storage cannot work without them.
        if len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must be set and at least 32 characters long.")
        if not self.token_encryption_key:
            raise ValueError("TOKEN_ENCRYPTION_KEY is required (a Fernet key).")
        if self.llm_provider == "groq" and not self.groq_api_key:
            raise ValueError("GROQ_API_KEY is required when LLM_PROVIDER=groq.")
        self._check_models_match_provider()
        if self.embedding_dim != 768:
            raise ValueError("EMBEDDING_DIM must be 768 (the vector columns are 768-dimensional).")
        if self.is_production:
            missing = [name.upper() for name in PRODUCTION_REQUIRED if not getattr(self, name)]
            if missing:
                raise ValueError(f"Missing required production settings: {', '.join(missing)}")
        return self

    def _check_models_match_provider(self) -> None:
        """Catch a provider switch that forgot the model names (the API would answer 404)."""
        for name in ("llm_fast_model", "llm_smart_model"):
            model = getattr(self, name)
            is_gemini = model.startswith("gemini")
            if self.llm_provider == "groq" and is_gemini:
                raise ValueError(
                    f"{name.upper()}={model} is a Gemini model but LLM_PROVIDER=groq. "
                    "Use a Groq model, e.g. openai/gpt-oss-120b."
                )
            if self.llm_provider == "google" and not is_gemini:
                raise ValueError(
                    f"{name.upper()}={model} is not a Gemini model but LLM_PROVIDER=google. "
                    "Use a Gemini model, e.g. gemini-3.1-flash-lite."
                )

    @property
    def is_production(self) -> bool:
        """True when running on Cloud Run in production mode."""
        return self.app_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS_ORIGINS split into a list (it is a comma-separated string)."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def oauth_redirect_uri(self) -> str:
        """Full Google OAuth redirect URI, reached through the web app's proxy."""
        return self.app_base_url.rstrip("/") + self.google_oauth_redirect_path

    @property
    def sync_checkpoint_url(self) -> str:
        """Session-pooler URL with the psycopg driver, used by Alembic."""
        return self.checkpoint_database_url.replace("postgresql://", "postgresql+psycopg://", 1)


@lru_cache
def get_settings() -> Settings:
    """Load settings once and reuse them."""
    return Settings()


settings = get_settings()
