"""Settings validation, error responses, health, cursors, OpenAPI operation ids."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.config import Settings
from app.errors import BadRequest, NotFound, register_error_handlers
from app.main import app
from app.services.emails import decode_cursor, encode_cursor

VALID = {
    "google_api_key": "key",
    "session_secret": "s" * 40,
    "token_encryption_key": "k",
    "_env_file": None,
}


def test_settings_require_groq_key_for_groq() -> None:
    with pytest.raises(ValidationError, match="GROQ_API_KEY"):
        Settings(**VALID, llm_provider="groq", groq_api_key="")


def test_settings_reject_gemini_models_with_groq() -> None:
    gemini = "gemini-3.1-flash-lite"
    with pytest.raises(ValidationError, match=r"LLM_FAST_MODEL=.* is a Gemini model"):
        Settings(**VALID, llm_provider="groq", groq_api_key="g", llm_fast_model=gemini)


def test_settings_reject_groq_models_with_google() -> None:
    with pytest.raises(ValidationError, match="not a Gemini model"):
        Settings(**VALID, llm_provider="google", llm_smart_model="openai/gpt-oss-120b")


def test_settings_accept_groq_models_with_groq() -> None:
    model = "openai/gpt-oss-120b"
    settings = Settings(
        **VALID, llm_provider="groq", groq_api_key="g", llm_fast_model=model, llm_smart_model=model
    )
    assert settings.llm_fast_model == model


def test_settings_require_google_key() -> None:
    with pytest.raises(ValidationError, match="GOOGLE_API_KEY"):
        Settings(**{**VALID, "google_api_key": ""})


def test_settings_production_fails_fast_on_missing_secrets() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(**VALID, app_env="production")


def test_settings_derived_values() -> None:
    settings = Settings(
        **VALID, cors_origins="http://a.com, http://b.com", app_base_url="http://x/"
    )
    assert settings.cors_origin_list == ["http://a.com", "http://b.com"]
    assert settings.oauth_redirect_uri == "http://x/api/auth/google/callback"


async def test_health() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.json() == {"status": "ok"}


async def test_error_handlers_return_standard_shape() -> None:
    test_app = FastAPI()
    register_error_handlers(test_app)

    @test_app.get("/missing")
    async def missing() -> None:
        raise NotFound("Email not found.")

    @test_app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret internal detail")

    @test_app.get("/number/{value}")
    async def number(value: int) -> int:
        return value

    transport = ASGITransport(app=test_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/missing")).json() == {
            "error": {"code": "not_found", "message": "Email not found."}
        }
        crash_response = await client.get("/crash")
        assert crash_response.status_code == 500
        assert "secret" not in crash_response.text
        invalid = await client.get("/number/abc")
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "validation_error"
        assert (await client.get("/nope")).json()["error"]["code"] == "http_error"


def test_cursor_round_trip() -> None:
    moment, email_id = datetime(2026, 11, 1, 8, 30, tzinfo=UTC), uuid.uuid4()
    assert decode_cursor(encode_cursor(moment, email_id)) == (moment, email_id)


def test_bad_cursor_is_bad_request() -> None:
    with pytest.raises(BadRequest):
        decode_cursor("not-a-cursor")


def test_operation_ids_are_function_names() -> None:
    operation_ids = {
        operation["operationId"]
        for path in app.openapi()["paths"].values()
        for operation in path.values()
    }
    assert {"get_health", "get_me", "list_emails", "get_email", "sync_now"} <= operation_ids
