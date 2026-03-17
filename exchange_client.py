import logging
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from urllib3.exceptions import InsecureRequestWarning

from exchangelib import Account, Configuration, Credentials, DELEGATE, FileAttachment
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter


LOGGER = logging.getLogger(__name__)


class ExchangeClientError(Exception):
    """Raised when the Exchange backend cannot fulfil a request."""


@dataclass
class AttachmentPayload:
    attachment_id: Optional[str]
    name: str
    content_type: str
    size_bytes: int
    is_inline: bool
    content: bytes


@dataclass
class MessagePayload:
    id: str
    subject: str
    sender: str
    datetime_received: str
    is_read: bool
    body: str
    attachments: List[AttachmentPayload] = field(default_factory=list)

    def to_summary(self) -> Dict[str, object]:
        body_preview = self.body[:500] if self.body else None
        return {
            "id": self.id,
            "subject": self.subject,
            "from": self.sender,
            "datetime_received": self.datetime_received,
            "is_read": self.is_read,
            "body_preview": body_preview,
        }

    def to_full(self) -> Dict[str, object]:
        data = self.to_summary()
        data["body"] = self.body
        return data


def _to_iso(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _sender_to_string(item) -> str:
    sender = getattr(item, "sender", None) or getattr(item, "author", None)
    return str(sender) if sender else ""


def _body_to_string(item) -> str:
    body = getattr(item, "text_body", None) or getattr(item, "body", None) or ""
    return str(body)


class LiveExchangeClient:
    def __init__(self) -> None:
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
        warnings.simplefilter("ignore", InsecureRequestWarning)

        username = os.getenv("EXCHANGE_USER")
        password = os.getenv("EXCHANGE_PASSWORD")
        service_endpoint = os.getenv("EXCHANGE_URL")
        mailbox = os.getenv("MAILBOX")

        missing = [
            name
            for name, value in (
                ("EXCHANGE_USER", username),
                ("EXCHANGE_PASSWORD", password),
                ("EXCHANGE_URL", service_endpoint),
                ("MAILBOX", mailbox),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing required Exchange configuration: " + ", ".join(missing)
            )

        self.mailbox = mailbox
        creds = Credentials(username=username, password=password)
        config = Configuration(
            service_endpoint=service_endpoint,
            credentials=creds,
            auth_type="NTLM",
        )
        self._account = Account(
            primary_smtp_address=mailbox,
            config=config,
            autodiscover=False,
            access_type=DELEGATE,
        )

    def _get_items(self, limit: int, only_unread: bool):
        query = self._account.inbox.all().order_by("-datetime_received")
        if only_unread:
            query = query.filter(is_read=False)
        return query[:limit]

    def _item_to_payload(
        self, item, include_attachments: bool = False, include_inline: bool = True
    ) -> MessagePayload:
        payload = MessagePayload(
            id=item.id,
            subject=getattr(item, "subject", "") or "",
            sender=_sender_to_string(item),
            datetime_received=_to_iso(getattr(item, "datetime_received", None)),
            is_read=bool(getattr(item, "is_read", False)),
            body=_body_to_string(item),
        )
        if not include_attachments:
            return payload

        for attachment in getattr(item, "attachments", []) or []:
            if not isinstance(attachment, FileAttachment):
                LOGGER.warning(
                    "Skipping unsupported attachment class",
                    extra={"message_id": item.id, "type": type(attachment).__name__},
                )
                continue

            is_inline = bool(getattr(attachment, "is_inline", False))
            if is_inline and not include_inline:
                continue

            try:
                content = bytes(attachment.content or b"")
            except Exception:
                LOGGER.exception(
                    "Failed to fetch attachment content",
                    extra={"message_id": item.id, "name": getattr(attachment, "name", "")},
                )
                continue

            attachment_id = getattr(attachment, "attachment_id", None)
            attachment_id = str(attachment_id) if attachment_id else None
            size_bytes = int(getattr(attachment, "size", 0) or len(content))
            payload.attachments.append(
                AttachmentPayload(
                    attachment_id=attachment_id,
                    name=getattr(attachment, "name", "") or "attachment.bin",
                    content_type=(
                        getattr(attachment, "content_type", "")
                        or "application/octet-stream"
                    ),
                    size_bytes=size_bytes,
                    is_inline=is_inline,
                    content=content,
                )
            )
        return payload

    def list_messages(self, limit: int, only_unread: bool) -> List[Dict[str, object]]:
        try:
            items = self._get_items(limit=limit, only_unread=only_unread)
            return [self._item_to_payload(item).to_summary() for item in items]
        except Exception as exc:  # exchangelib raises many custom exceptions
            raise ExchangeClientError("Unable to fetch messages from Exchange") from exc

    def get_message(self, message_id: str) -> Dict[str, object]:
        try:
            item = self._account.inbox.get(id=message_id)
            return self._item_to_payload(item).to_full()
        except Exception as exc:  # exchangelib raises many custom exceptions
            raise ExchangeClientError("Unable to fetch message from Exchange") from exc

    def list_messages_for_sync(
        self, limit: int, only_unread: bool, include_inline: bool
    ) -> List[MessagePayload]:
        try:
            items = self._get_items(limit=limit, only_unread=only_unread)
            return [
                self._item_to_payload(
                    item,
                    include_attachments=True,
                    include_inline=include_inline,
                )
                for item in items
            ]
        except Exception as exc:  # exchangelib raises many custom exceptions
            raise ExchangeClientError(
                "Unable to fetch attachment data from Exchange"
            ) from exc


@dataclass
class MockAttachment:
    name: str
    content_type: str
    is_inline: bool
    content: bytes
    attachment_id: Optional[str] = None

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass
class MockMessage:
    id: str
    subject: str
    sender: str
    datetime_received: str
    is_read: bool
    body: str
    attachments: List[MockAttachment] = field(default_factory=list)


class MockExchangeClient:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.mailbox = os.getenv("MAILBOX", "mock-mailbox@example.com")
        self._messages: List[MockMessage] = [
            MockMessage(
                id="MOCK-1",
                subject="Mock trade confirmation",
                sender="tradebot@example.com",
                datetime_received=(now - timedelta(hours=1)).isoformat(),
                is_read=False,
                body="Order #12345 has been executed successfully.",
                attachments=[
                    MockAttachment(
                        name="trade-confirmation.pdf",
                        content_type="application/pdf",
                        is_inline=False,
                        content=b"%PDF-mock-content-for-confirmation",
                        attachment_id="M1-A1",
                    ),
                    MockAttachment(
                        name="logo-inline.png",
                        content_type="image/png",
                        is_inline=True,
                        content=b"\x89PNG-mock-inline-logo",
                        attachment_id="M1-A2",
                    ),
                ],
            ),
            MockMessage(
                id="MOCK-2",
                subject="Mock daily summary",
                sender="reports@example.com",
                datetime_received=(now - timedelta(hours=5)).isoformat(),
                is_read=True,
                body="Daily summary for your mock account.",
                attachments=[],
            ),
            MockMessage(
                id="MOCK-3",
                subject="Mock onboarding",
                sender="support@example.com",
                datetime_received=(now - timedelta(days=1)).isoformat(),
                is_read=False,
                body="Welcome to the mock Exchange environment!",
                attachments=[
                    MockAttachment(
                        name="welcome-inline.txt",
                        content_type="text/plain",
                        is_inline=True,
                        content=b"inline welcome",
                        attachment_id="M3-A1",
                    )
                ],
            ),
        ]

    def list_messages(self, limit: int, only_unread: bool) -> List[Dict[str, object]]:
        messages = self._messages
        if only_unread:
            messages = [message for message in messages if not message.is_read]
        messages = messages[:limit]
        return [
            MessagePayload(
                id=message.id,
                subject=message.subject,
                sender=message.sender,
                datetime_received=message.datetime_received,
                is_read=message.is_read,
                body=message.body,
            ).to_summary()
            for message in messages
        ]

    def get_message(self, message_id: str) -> Dict[str, object]:
        for message in self._messages:
            if message.id == message_id:
                return MessagePayload(
                    id=message.id,
                    subject=message.subject,
                    sender=message.sender,
                    datetime_received=message.datetime_received,
                    is_read=message.is_read,
                    body=message.body,
                ).to_full()
        raise ExchangeClientError(f"Message {message_id} not found in mock inbox")

    def list_messages_for_sync(
        self, limit: int, only_unread: bool, include_inline: bool
    ) -> List[MessagePayload]:
        messages = self._messages
        if only_unread:
            messages = [message for message in messages if not message.is_read]
        messages = messages[:limit]

        payloads: List[MessagePayload] = []
        for message in messages:
            payload = MessagePayload(
                id=message.id,
                subject=message.subject,
                sender=message.sender,
                datetime_received=message.datetime_received,
                is_read=message.is_read,
                body=message.body,
            )
            for attachment in message.attachments:
                if attachment.is_inline and not include_inline:
                    continue
                payload.attachments.append(
                    AttachmentPayload(
                        attachment_id=attachment.attachment_id,
                        name=attachment.name,
                        content_type=attachment.content_type,
                        size_bytes=attachment.size_bytes,
                        is_inline=attachment.is_inline,
                        content=attachment.content,
                    )
                )
            payloads.append(payload)
        return payloads


def create_exchange_client(backend: Optional[str] = None):
    backend_name = (backend or os.getenv("EXCHANGE_BACKEND", "mock")).lower()
    if backend_name == "live":
        return LiveExchangeClient()
    if backend_name == "mock":
        return MockExchangeClient()
    raise RuntimeError("EXCHANGE_BACKEND must be either 'live' or 'mock'")
