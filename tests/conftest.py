"""Test configuration, loaded before the app is imported.

Integration tests use TEST_DATABASE_URL (env var or .env) and run in a throwaway
`test_suite` schema, so they never touch real data. Without it they are skipped.
"""

import os

from dotenv import dotenv_values

TEST_SCHEMA = "test_suite"

_test_database_url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(".env").get(
    "TEST_DATABASE_URL"
)
if _test_database_url:
    os.environ["DATABASE_URL"] = _test_database_url
    os.environ["DATABASE_SCHEMA"] = TEST_SCHEMA

# Tests must never start real Gmail watches or depend on the developer's environment.
os.environ["PUBSUB_TOPIC"] = ""
os.environ["APP_ENV"] = "development"
os.environ["LLM_PROVIDER"] = "google"
os.environ["LLM_FAST_MODEL"] = os.environ["LLM_SMART_MODEL"] = "gemini-3.1-flash-lite"
os.environ["MAX_EMAILS_PER_RUN"] = "10"

HAS_TEST_DATABASE = bool(_test_database_url)


import pytest  # noqa: E402  (env vars above must be set before anything imports the app)


class RealLLMCallError(AssertionError):
    """Raised when a test tries to call a real model (tests must use fakes)."""


@pytest.fixture(autouse=True)
def block_real_llm_calls(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach Gemini or Groq: model objects can be built, but not called.

    The only exception is tests/scenarios (marked `real_llm`), the on-demand evaluation suite.
    """
    if request.node.get_closest_marker("real_llm"):
        return
    from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
    from langchain_groq import ChatGroq

    def refuse(*args: object, **kwargs: object) -> None:
        raise RealLLMCallError("A test tried to call a real LLM / embeddings API.")

    for chat_class in (ChatGoogleGenerativeAI, ChatGroq):
        monkeypatch.setattr(chat_class, "_agenerate", refuse)
        monkeypatch.setattr(chat_class, "_generate", refuse)
    for method in ("aembed_documents", "aembed_query", "embed_documents", "embed_query"):
        monkeypatch.setattr(GoogleGenerativeAIEmbeddings, method, refuse)
