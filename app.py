import os
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from urllib3.exceptions import InsecureRequestWarning

from exchangelib import Account, Configuration, Credentials, DELEGATE
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter


load_dotenv()


class ExchangeClientError(Exception):
    """Raised when the Exchange backend cannot fulfil a request."""


def _serialize_message_summary(item) -> Dict[str, object]:
    sender = getattr(item, "sender", None) or getattr(item, "author", None)
    sender_str = str(sender) if sender else ""
    text_body = getattr(item, "text_body", None)
    body_preview = None
    if text_body:
        body_preview = text_body[:500]
    elif getattr(item, "body", None):
        body_preview = str(item.body)[:500]

    datetime_received = getattr(item, "datetime_received", None)
    if datetime_received:
        datetime_received = datetime_received.isoformat()

    return {
        "id": item.id,
        "subject": getattr(item, "subject", ""),
        "from": sender_str,
        "datetime_received": datetime_received,
        "is_read": bool(getattr(item, "is_read", False)),
        "body_preview": body_preview,
    }


def _serialize_message_full(item) -> Dict[str, object]:
    data = _serialize_message_summary(item)
    body = getattr(item, "text_body", None) or getattr(item, "body", None) or ""
    data["body"] = str(body)
    return data


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

    def list_messages(self, limit: int, only_unread: bool) -> List[Dict[str, object]]:
        try:
            query = self._account.inbox.all().order_by("-datetime_received")
            if only_unread:
                query = query.filter(is_read=False)
            messages = query[:limit]
            return [_serialize_message_summary(message) for message in messages]
        except Exception as exc:  # exchangelib raises many custom exceptions
            raise ExchangeClientError("Unable to fetch messages from Exchange") from exc

    def get_message(self, message_id: str) -> Dict[str, object]:
        try:
            item = self._account.inbox.get(id=message_id)
        except Exception as exc:  # exchangelib raises many custom exceptions
            raise ExchangeClientError("Unable to fetch message from Exchange") from exc
        return _serialize_message_full(item)


@dataclass
class MockMessage:
    id: str
    subject: str
    sender: str
    datetime_received: str
    is_read: bool
    body: str

    @property
    def body_preview(self) -> str:
        return self.body[:500]


class MockExchangeClient:
    def __init__(self) -> None:
        now = datetime.utcnow()
        self._messages: List[MockMessage] = [
            MockMessage(
                id="MOCK-1",
                subject="Mock trade confirmation",
                sender="tradebot@example.com",
                datetime_received=(now - timedelta(hours=1)).isoformat(),
                is_read=False,
                body="Order #12345 has been executed successfully.",
            ),
            MockMessage(
                id="MOCK-2",
                subject="Mock daily summary",
                sender="reports@example.com",
                datetime_received=(now - timedelta(hours=5)).isoformat(),
                is_read=True,
                body="Daily summary for your mock account.",
            ),
            MockMessage(
                id="MOCK-3",
                subject="Mock onboarding",
                sender="support@example.com",
                datetime_received=(now - timedelta(days=1)).isoformat(),
                is_read=False,
                body="Welcome to the mock Exchange environment!",
            ),
        ]

    def list_messages(self, limit: int, only_unread: bool) -> List[Dict[str, object]]:
        messages = self._messages
        if only_unread:
            messages = [message for message in messages if not message.is_read]
        messages = messages[:limit]
        return [
            {
                "id": message.id,
                "subject": message.subject,
                "from": message.sender,
                "datetime_received": message.datetime_received,
                "is_read": message.is_read,
                "body_preview": message.body_preview,
            }
            for message in messages
        ]

    def get_message(self, message_id: str) -> Dict[str, object]:
        for message in self._messages:
            if message.id == message_id:
                return {
                    "id": message.id,
                    "subject": message.subject,
                    "from": message.sender,
                    "datetime_received": message.datetime_received,
                    "is_read": message.is_read,
                    "body": message.body,
                }
        raise ExchangeClientError(f"Message {message_id} not found in mock inbox")


PORT = int(os.getenv("PORT", "8765"))
backend = os.getenv("EXCHANGE_BACKEND", "mock").lower()

if backend == "live":
    exchange_client = LiveExchangeClient()
elif backend == "mock":
    exchange_client = MockExchangeClient()
else:
    raise RuntimeError(
        "EXCHANGE_BACKEND must be either 'live' or 'mock'"
    )

app = Flask(__name__)


@app.get("/inbox")
def inbox() -> tuple:
    limit = int(request.args.get("limit", "5"))
    only_unread = request.args.get("only_unread", "0") == "1"

    try:
        items = exchange_client.list_messages(limit=limit, only_unread=only_unread)
    except ExchangeClientError as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(items)


@app.get("/message")
def get_message() -> tuple:
    message_id = request.args.get("id")
    if not message_id:
        return jsonify({"error": "id parameter required"}), 400

    try:
        item = exchange_client.get_message(message_id)
    except ExchangeClientError as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(item)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
