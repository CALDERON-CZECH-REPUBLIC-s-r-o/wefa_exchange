import hashlib
import logging
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


LOGGER = logging.getLogger(__name__)

FILENAME_INVALID_RE = re.compile(r"[\\/\x00-\x1f\x7f]")
PATH_COMPONENT_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class SaveResult:
    saved: bool
    sha256: str
    stored_path: Optional[str]
    skipped_reason: Optional[str] = None


def sanitize_filename(name: str, max_length: int = 120) -> str:
    value = (name or "").strip()
    value = FILENAME_INVALID_RE.sub("_", value)
    value = value.lstrip(".")
    if not value:
        value = "attachment.bin"

    if len(value) <= max_length:
        return value

    stem, ext = os.path.splitext(value)
    ext = ext[:20]
    keep = max_length - len(ext)
    if keep <= 0:
        return value[:max_length]
    return f"{stem[:keep]}{ext}"


def sanitize_path_component(value: str, fallback: str, max_length: int = 80) -> str:
    cleaned = (value or "").strip()
    cleaned = PATH_COMPONENT_INVALID_RE.sub("_", cleaned)
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime_or_now(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return datetime.now(timezone.utc)


class AttachmentStore:
    def __init__(
        self,
        attachments_root: str,
        db_path: str,
        mailbox: str,
        max_bytes: int,
    ) -> None:
        self.attachments_root = os.path.abspath(attachments_root)
        self.db_path = os.path.abspath(db_path)
        self.mailbox_component = sanitize_path_component(
            mailbox, fallback="mailbox", max_length=120
        )
        self.max_bytes = int(max_bytes)

        os.makedirs(self.attachments_root, mode=0o750, exist_ok=True)
        os.makedirs(os.path.dirname(self.db_path), mode=0o750, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    subject TEXT,
                    sender TEXT,
                    datetime_received TEXT,
                    is_read INTEGER,
                    last_seen_at TEXT
                );

                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    attachment_id TEXT,
                    name TEXT NOT NULL,
                    content_type TEXT,
                    size_bytes INTEGER,
                    is_inline INTEGER,
                    sha256 TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    UNIQUE (message_id, sha256, name),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_attachments_message_id
                    ON attachments(message_id);
                CREATE INDEX IF NOT EXISTS idx_attachments_stored_at
                    ON attachments(stored_at DESC);
                """
            )

    def _upsert_message_conn(self, conn: sqlite3.Connection, message) -> None:
        conn.execute(
            """
            INSERT INTO messages (
                message_id, subject, sender, datetime_received, is_read, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                subject=excluded.subject,
                sender=excluded.sender,
                datetime_received=excluded.datetime_received,
                is_read=excluded.is_read,
                last_seen_at=excluded.last_seen_at
            """,
            (
                message.id,
                message.subject,
                message.sender,
                message.datetime_received,
                int(bool(message.is_read)),
                _utcnow_iso(),
            ),
        )

    def upsert_message(self, message) -> None:
        with self._connect() as conn:
            self._upsert_message_conn(conn, message)

    def _build_message_dir(self, message_id: str, datetime_received: str) -> str:
        received_at = _parse_datetime_or_now(datetime_received)
        year = received_at.strftime("%Y")
        month = received_at.strftime("%m")
        safe_message_id = sanitize_path_component(
            message_id, fallback="message", max_length=120
        )
        return os.path.join(
            self.attachments_root,
            self.mailbox_component,
            year,
            month,
            safe_message_id,
        )

    def build_attachment_path(
        self, message_id: str, datetime_received: str, sha256: str, attachment_name: str
    ) -> str:
        safe_name = sanitize_filename(attachment_name)
        target_dir = self._build_message_dir(message_id, datetime_received)
        file_name = f"{sha256}_{safe_name}"
        path = os.path.abspath(os.path.join(target_dir, file_name))

        root = Path(self.attachments_root)
        resolved = Path(path)
        if root not in resolved.parents:
            raise ValueError("Blocked path traversal attempt")
        return path

    def _write_file_atomic(self, destination: str, content: bytes) -> None:
        os.makedirs(os.path.dirname(destination), mode=0o750, exist_ok=True)
        if os.path.exists(destination):
            return

        fd, temp_path = tempfile.mkstemp(
            dir=os.path.dirname(destination),
            prefix=".tmp-attachment-",
            suffix=".part",
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, destination)
            os.chmod(destination, 0o640)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def save_attachment(self, message, attachment) -> SaveResult:
        if attachment.size_bytes > self.max_bytes:
            LOGGER.info(
                "Skipping attachment above size limit",
                extra={
                    "message_id": message.id,
                    "name": attachment.name,
                    "size_bytes": attachment.size_bytes,
                    "max_bytes": self.max_bytes,
                },
            )
            return SaveResult(
                saved=False,
                sha256="",
                stored_path=None,
                skipped_reason="max_bytes_exceeded",
            )

        digest = hashlib.sha256(attachment.content).hexdigest()
        with self._connect() as conn:
            self._upsert_message_conn(conn, message)
            existing = conn.execute(
                """
                SELECT stored_path
                FROM attachments
                WHERE message_id = ? AND sha256 = ? AND name = ?
                """,
                (message.id, digest, attachment.name),
            ).fetchone()
            if existing:
                return SaveResult(
                    saved=False,
                    sha256=digest,
                    stored_path=existing["stored_path"],
                    skipped_reason="duplicate",
                )

            destination = self.build_attachment_path(
                message_id=message.id,
                datetime_received=message.datetime_received,
                sha256=digest,
                attachment_name=attachment.name,
            )
            self._write_file_atomic(destination, attachment.content)
            conn.execute(
                """
                INSERT INTO attachments (
                    message_id, attachment_id, name, content_type, size_bytes,
                    is_inline, sha256, stored_path, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    attachment.attachment_id,
                    attachment.name,
                    attachment.content_type,
                    attachment.size_bytes,
                    int(bool(attachment.is_inline)),
                    digest,
                    destination,
                    _utcnow_iso(),
                ),
            )

        LOGGER.info(
            "Attachment saved",
            extra={
                "message_id": message.id,
                "name": attachment.name,
                "size_bytes": attachment.size_bytes,
                "sha256": digest,
                "stored_path": destination,
            },
        )
        return SaveResult(
            saved=True,
            sha256=digest,
            stored_path=destination,
            skipped_reason=None,
        )

    def list_attachments(self, message_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, object]]:
        limit = max(1, min(int(limit), 1000))
        sql = (
            "SELECT message_id, attachment_id, name, content_type, size_bytes, "
            "is_inline, sha256, stored_path, stored_at "
            "FROM attachments"
        )
        params: tuple = ()
        if message_id:
            sql += " WHERE message_id = ?"
            params = (message_id,)
        sql += " ORDER BY stored_at DESC, id DESC LIMIT ?"
        params = (*params, limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_attachment_dict(row) for row in rows]

    def get_attachments_for_message(self, message_id: str) -> List[Dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, attachment_id, name, content_type, size_bytes,
                       is_inline, sha256, stored_path, stored_at
                FROM attachments
                WHERE message_id = ?
                ORDER BY stored_at DESC, id DESC
                """,
                (message_id,),
            ).fetchall()
        return [self._row_to_attachment_dict(row) for row in rows]

    def set_sync_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    @staticmethod
    def _row_to_attachment_dict(row: sqlite3.Row) -> Dict[str, object]:
        return {
            "message_id": row["message_id"],
            "attachment_id": row["attachment_id"],
            "name": row["name"],
            "content_type": row["content_type"],
            "size_bytes": row["size_bytes"],
            "is_inline": bool(row["is_inline"]),
            "sha256": row["sha256"],
            "stored_path": row["stored_path"],
            "stored_at": row["stored_at"],
        }
