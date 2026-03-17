from pathlib import Path

import pytest

from app import create_app
from attachments_store import AttachmentStore
from exchange_client import MockExchangeClient


@pytest.fixture
def client(tmp_path):
    store = AttachmentStore(
        attachments_root=str(tmp_path / "attachments"),
        db_path=str(tmp_path / "state" / "attachments.sqlite3"),
        mailbox="mock-mailbox@example.com",
        max_bytes=1024 * 1024,
    )
    app = create_app(exchange_client=MockExchangeClient(), attachment_store=store)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def test_sync_and_list_attachments_contract(client) -> None:
    sync = client.post("/attachments/sync?limit=10&only_unread=0&include_inline=1")
    assert sync.status_code == 200
    payload = sync.get_json()

    for key in (
        "messages_scanned",
        "messages_with_attachments",
        "attachments_saved",
        "attachments_skipped",
        "errors",
    ):
        assert key in payload

    assert payload["messages_scanned"] == 3
    assert payload["attachments_saved"] == 3
    assert payload["errors"] == 0

    listed = client.get("/attachments?limit=100")
    assert listed.status_code == 200
    rows = listed.get_json()
    assert len(rows) == 3

    filtered = client.get("/attachments?message_id=MOCK-1&limit=100")
    assert filtered.status_code == 200
    filtered_rows = filtered.get_json()
    assert len(filtered_rows) == 2
    assert all(row["message_id"] == "MOCK-1" for row in filtered_rows)


def test_inline_filter_and_idempotent_sync(client) -> None:
    first = client.post("/attachments/sync?limit=10&only_unread=0&include_inline=0")
    assert first.status_code == 200
    first_data = first.get_json()
    assert first_data["attachments_saved"] == 1

    second = client.post("/attachments/sync?limit=10&only_unread=0&include_inline=1")
    assert second.status_code == 200
    second_data = second.get_json()
    assert second_data["attachments_saved"] == 2
    assert second_data["attachments_skipped"] == 1
    assert second_data["errors"] == 0


def test_message_includes_attachment_metadata(client) -> None:
    sync = client.post("/attachments/sync?limit=10&only_unread=0&include_inline=1")
    assert sync.status_code == 200

    message = client.get("/message?id=MOCK-1")
    assert message.status_code == 200
    payload = message.get_json()
    assert "attachments" in payload
    assert len(payload["attachments"]) == 2

    for entry in payload["attachments"]:
        assert "name" in entry
        assert "size_bytes" in entry
        assert "content_type" in entry
        assert "is_inline" in entry
        assert "sha256" in entry
        assert "stored_path" in entry
        assert Path(entry["stored_path"]).exists()
