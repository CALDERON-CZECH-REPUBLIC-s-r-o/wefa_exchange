from pathlib import Path

from attachments_store import AttachmentStore, sanitize_filename
from exchange_client import AttachmentPayload, MessagePayload


def _message() -> MessagePayload:
    return MessagePayload(
        id="MSG-1",
        subject="Subject",
        sender="sender@example.com",
        datetime_received="2026-02-01T10:00:00+00:00",
        is_read=False,
        body="Body",
    )


def _attachment(name: str, content: bytes) -> AttachmentPayload:
    return AttachmentPayload(
        attachment_id="A-1",
        name=name,
        content_type="text/plain",
        size_bytes=len(content),
        is_inline=False,
        content=content,
    )


def test_sanitize_filename_strips_unsafe_bits() -> None:
    safe = sanitize_filename("../.hidden\\..\x00payload.txt")
    assert "/" not in safe
    assert "\\" not in safe
    assert "\x00" not in safe
    assert not safe.startswith(".")


def test_build_attachment_path_stays_under_root(tmp_path) -> None:
    store = AttachmentStore(
        attachments_root=str(tmp_path / "attachments"),
        db_path=str(tmp_path / "state" / "attachments.sqlite3"),
        mailbox="mailbox@example.com",
        max_bytes=1024 * 1024,
    )

    path = store.build_attachment_path(
        message_id="../../etc/passwd",
        datetime_received="2026-02-01T10:00:00+00:00",
        sha256="a" * 64,
        attachment_name="../../secrets.txt",
    )
    resolved = Path(path).resolve()
    assert resolved.is_relative_to(Path(store.attachments_root).resolve())


def test_duplicate_attachments_are_idempotent(tmp_path) -> None:
    store = AttachmentStore(
        attachments_root=str(tmp_path / "attachments"),
        db_path=str(tmp_path / "state" / "attachments.sqlite3"),
        mailbox="mailbox@example.com",
        max_bytes=1024 * 1024,
    )

    message = _message()
    attachment = _attachment("report.txt", b"same content")

    first = store.save_attachment(message, attachment)
    second = store.save_attachment(message, attachment)

    assert first.saved is True
    assert second.saved is False
    assert second.skipped_reason == "duplicate"

    rows = store.get_attachments_for_message(message.id)
    assert len(rows) == 1
    assert Path(rows[0]["stored_path"]).exists()
