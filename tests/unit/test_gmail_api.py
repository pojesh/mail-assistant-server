"""Gmail wrapper logic that does not need the network: label filtering and history paging."""

import uuid
from typing import Any

import pytest

from app.errors import NotFound
from app.google import gmail_api

USER_ID = uuid.uuid4()


class FakeHistoryApi:
    """Returns the given pages for users().history().list(...).execute()."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.requests: list[dict[str, Any]] = []

    def users(self) -> "FakeHistoryApi":
        return self

    def history(self) -> "FakeHistoryApi":
        return self

    def list(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return self.pages[len(self.requests) - 1]


def added(message_id: str, labels: list[str]) -> dict[str, Any]:
    return {"messagesAdded": [{"message": {"id": message_id, "labelIds": labels}}]}


@pytest.fixture
def fake_gmail(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(pages: list[dict[str, Any]], error: Exception | None = None) -> FakeHistoryApi:
        api = FakeHistoryApi(pages)

        async def get_gmail(_: uuid.UUID) -> FakeHistoryApi:
            return api

        async def run_google(request: Any) -> Any:
            if error:
                raise error
            return request

        monkeypatch.setattr(gmail_api, "get_gmail", get_gmail)
        monkeypatch.setattr(gmail_api, "run_google", run_google)
        return api

    return install


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["INBOX", "UNREAD"], True),
        (["SENT"], True),
        (["INBOX", "SPAM"], False),
        (["DRAFT"], False),
        (["CATEGORY_UPDATES"], False),  # archived-only mail is not stored
        ([], False),
    ],
)
def test_is_relevant(labels: list[str], expected: bool) -> None:
    assert gmail_api.is_relevant(labels) is expected


async def test_list_history_collects_all_pages(fake_gmail: Any) -> None:
    pages = [
        {
            "historyId": "105",
            "nextPageToken": "p2",
            "history": [added("a", ["INBOX"]), added("spam", ["SPAM"])],
        },
        {
            "historyId": "110",
            "history": [
                added("b", ["SENT"]),
                added("a", ["INBOX"]),  # duplicates are dropped
                {"labelsRemoved": [{"message": {"id": "a"}, "labelIds": ["UNREAD"]}]},
                {"labelsAdded": [{"message": {"id": "c"}, "labelIds": ["UNREAD"]}]},
            ],
        },
    ]
    api = fake_gmail(pages)
    changes = await gmail_api.list_history(USER_ID, "100")
    assert changes.added_message_ids == ["a", "b"]
    assert changes.read_message_ids == ["a"]
    assert changes.unread_message_ids == ["c"]
    assert changes.latest_history_id == "110"
    assert api.requests[1]["pageToken"] == "p2"


async def test_list_history_expired_on_404(fake_gmail: Any) -> None:
    fake_gmail([{}], error=NotFound())
    with pytest.raises(gmail_api.HistoryExpired):
        await gmail_api.list_history(USER_ID, "1")


async def test_list_history_too_many_pages(fake_gmail: Any) -> None:
    endless = [
        {"historyId": "2", "nextPageToken": "more", "history": []}
    ] * gmail_api.MAX_HISTORY_PAGES
    fake_gmail(endless)
    with pytest.raises(gmail_api.HistoryExpired):
        await gmail_api.list_history(USER_ID, "1")
