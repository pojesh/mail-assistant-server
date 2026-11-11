"""Scenario evaluation: real model calls on synthetic mail. Runs only when asked for:

    uv run pytest tests/scenarios                 # everything (about 20-30 min on a free tier)
    uv run pytest tests/scenarios -k triage       # one part (triage, scheduling, chat, flows)

Results: tests/scenarios/RESULTS.md and tests/scenarios/results/latest.json
(shown at http://localhost:3000/eval).

- The models under test come from .env (the app's real configuration).
- The judge for chat answers is HCL AI Cafe GPT-4.1 (see judge.py).
- Data lives in a throwaway `scenario_suite` schema in the LOCAL Docker database, never in
  Supabase. Gmail and Calendar are fakes: nothing is ever written to Google.
"""

import os
import sys

from dotenv import dotenv_values

LOCAL_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:55432/postgres"
SCENARIO_SCHEMA = "scenario_suite"

# Only touch the environment when this suite was asked for explicitly; `pytest tests` must
# leave the unit/integration configuration alone (those tests are skipped here instead).
EXPLICIT = any("scenarios" in argument for argument in sys.argv[1:])

if EXPLICIT:
    _env = dotenv_values(".env")
    for _key in ("LLM_PROVIDER", "LLM_FAST_MODEL", "LLM_SMART_MODEL", "LLM_REQUESTS_PER_MINUTE"):
        _value = os.environ.get(f"EVAL_{_key}") or _env.get(_key)
        if _value:
            os.environ[_key] = _value.split("#")[0].strip()
    os.environ["DATABASE_URL"] = os.environ.get("SCENARIO_DATABASE_URL", LOCAL_DATABASE_URL)
    os.environ["DATABASE_SCHEMA"] = SCENARIO_SCHEMA
    _host = os.environ["DATABASE_URL"].split("@")[-1]
    if not _host.startswith(("localhost", "127.0.0.1")):
        raise RuntimeError(f"Scenario tests only run against a local database, not {_host}.")


import time  # noqa: E402
from collections.abc import AsyncIterator, Iterator  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from contextvars import ContextVar  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import pytest  # noqa: E402

from tests.scenarios.recorder import Recorder  # noqa: E402


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark scenario tests as real-model tests, or skip them when not asked for."""
    skip = pytest.mark.skip(reason="Calls real models; run `uv run pytest tests/scenarios`.")
    for item in items:
        if "scenarios" in str(item.path):
            item.add_marker(pytest.mark.real_llm if EXPLICIT else skip)


# --- Timing ---------------------------------------------------------------------------------
# Wall-clock time would include waiting for our own rate limiter (free tiers allow only a few
# requests per minute), so the limiter is wrapped to measure that wait and count model calls.


@dataclass
class Meter:
    started: float = field(default_factory=time.perf_counter)
    waited: float = 0.0  # seconds spent waiting for the rate limiter
    calls: int = 0  # model calls (every call passes the limiter once)
    stopped: float | None = None

    def net(self) -> float:
        """Seconds so far, minus rate-limiter waiting: the latency a paid tier would see."""
        end = self.stopped if self.stopped is not None else time.perf_counter()
        return max(0.0, end - self.started - self.waited)


CURRENT_METER: ContextVar[Meter | None] = ContextVar("current_meter", default=None)


@contextmanager
def measure() -> Iterator[Meter]:
    meter = Meter()
    token = CURRENT_METER.set(meter)
    try:
        yield meter
    finally:
        meter.stopped = time.perf_counter()
        CURRENT_METER.reset(token)


@pytest.fixture(scope="session", autouse=True)
def meter_model_calls() -> Iterator[None]:
    from langchain_core.rate_limiters import InMemoryRateLimiter

    original = InMemoryRateLimiter.aacquire

    async def timed(self: InMemoryRateLimiter, *, blocking: bool = True) -> bool:
        started = time.perf_counter()
        acquired = await original(self, blocking=blocking)
        meter = CURRENT_METER.get()
        if meter is not None:
            meter.waited += time.perf_counter() - started
            meter.calls += 1
        return acquired

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(InMemoryRateLimiter, "aacquire", timed)
        yield


# --- Results ----------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def recorder() -> Iterator[Recorder]:
    recording = Recorder()
    yield recording
    if recording.has_results():
        paths = recording.write()
        print(f"\nScenario results written to {paths[0]} and {paths[1]}")


# --- Local database (chat and flow scenarios only) ---------------------------------------------


@pytest.fixture(scope="session")
async def scenario_database() -> AsyncIterator[None]:
    """Create all tables in the throwaway schema of the local Docker database, drop at the end."""
    from sqlalchemy import text

    from app.db import get_engine
    from app.models import Base

    engine = get_engine()
    assert engine.get_execution_options().get("schema_translate_map") == {None: SCENARIO_SCHEMA}
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {SCENARIO_SCHEMA} CASCADE"))
            await connection.execute(text(f"CREATE SCHEMA {SCENARIO_SCHEMA}"))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await connection.run_sync(Base.metadata.create_all)
    except OSError as error:
        pytest.skip(f"Local Docker database not reachable ({error}); start mail-assistant-testdb.")
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
            {"s": SCENARIO_SCHEMA},
        )
        missing = set(Base.metadata.tables) - {row[0] for row in rows}
    if missing:
        pytest.exit(f"Refusing to run: tables not isolated in {SCENARIO_SCHEMA}: {missing}", 2)
    yield
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP SCHEMA IF EXISTS {SCENARIO_SCHEMA} CASCADE"))
    await engine.dispose()


async def truncate_all() -> None:
    """Empty every table of the scenario schema (schema-qualified: never `public`)."""
    from sqlalchemy import text

    from app.db import get_engine
    from app.models import Base

    names = ", ".join(f'{SCENARIO_SCHEMA}."{table.name}"' for table in Base.metadata.sorted_tables)
    async with get_engine().begin() as connection:
        await connection.execute(text(f"TRUNCATE {names} CASCADE"))
