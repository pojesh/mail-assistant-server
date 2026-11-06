"""run_google: retries on 429/5xx and maps Google errors to our errors."""

from typing import Any

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app.errors import GoogleApiError, NeedsReconnect, NotFound
from app.google import clients
from app.google.clients import run_google


def http_error(status: int, content: str = "{}") -> HttpError:
    return HttpError(httplib2.Response({"status": status}), content.encode())


class FakeRequest:
    """Stands in for a googleapiclient request: raises the queued errors, then succeeds."""

    def __init__(self, *errors: Exception, result: Any = None) -> None:
        self.errors = list(errors)
        self.result = result if result is not None else {"ok": True}
        self.calls = 0

    def execute(self) -> Any:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr(clients.asyncio, "sleep", instant)


async def test_returns_result() -> None:
    assert await run_google(FakeRequest(result={"id": "1"})) == {"id": "1"}


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retries_transient_errors(status: int) -> None:
    request = FakeRequest(http_error(status), http_error(status))
    assert await run_google(request) == {"ok": True}
    assert request.calls == 3


async def test_retries_gmail_rate_limit_403() -> None:
    request = FakeRequest(
        http_error(403, '{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}')
    )
    assert await run_google(request) == {"ok": True}


async def test_gives_up_after_two_retries() -> None:
    request = FakeRequest(http_error(503), http_error(503), http_error(503))
    with pytest.raises(GoogleApiError):
        await run_google(request)
    assert request.calls == 3


async def test_maps_401_to_needs_reconnect() -> None:
    with pytest.raises(NeedsReconnect):
        await run_google(FakeRequest(http_error(401)))


async def test_maps_refresh_error_to_needs_reconnect() -> None:
    with pytest.raises(NeedsReconnect):
        await run_google(FakeRequest(RefreshError("invalid_grant")))


async def test_maps_404_to_not_found_without_retry() -> None:
    request = FakeRequest(http_error(404))
    with pytest.raises(NotFound):
        await run_google(request)
    assert request.calls == 1


async def test_permission_error_is_not_retried() -> None:
    request = FakeRequest(http_error(403, '{"error": "forbidden"}'))
    with pytest.raises(GoogleApiError):
        await run_google(request)
    assert request.calls == 1


async def test_retries_network_errors() -> None:
    request = FakeRequest(TimeoutError(), ConnectionResetError())
    assert await run_google(request) == {"ok": True}
