import logging
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from attachments_store import AttachmentStore
from exchange_client import ExchangeClientError, create_exchange_client


load_dotenv()

LOGGER = logging.getLogger(__name__)


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _parse_int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_bool_arg(name: str, default: bool) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be 0 or 1")


def _resolve_storage_paths() -> tuple[str, str]:
    root_env = os.getenv("ATTACHMENTS_ROOT")
    db_env = os.getenv("ATTACHMENTS_DB_PATH")
    if root_env and db_env:
        return root_env, db_env

    default_root = "/data/attachments"
    default_db = "/data/state/attachments.sqlite3"
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return root_env or default_root, db_env or default_db

    return (
        root_env or "/tmp/wefa-exchange/attachments",
        db_env or "/tmp/wefa-exchange/state/attachments.sqlite3",
    )


def create_app(exchange_client=None, attachment_store: Optional[AttachmentStore] = None) -> Flask:
    _configure_logging()

    exchange = exchange_client or create_exchange_client()
    mailbox = os.getenv("MAILBOX", "mailbox@example.com")
    attachments_root, attachments_db_path = _resolve_storage_paths()
    attachments_max_bytes = int(os.getenv("ATTACHMENTS_MAX_BYTES", "26214400"))
    include_inline_default = os.getenv("ATTACHMENTS_INCLUDE_INLINE", "1") == "1"

    store = attachment_store or AttachmentStore(
        attachments_root=attachments_root,
        db_path=attachments_db_path,
        mailbox=mailbox,
        max_bytes=attachments_max_bytes,
    )

    app = Flask(__name__)
    app.config["EXCHANGE_CLIENT"] = exchange
    app.config["ATTACHMENT_STORE"] = store
    app.config["ATTACHMENTS_INCLUDE_INLINE_DEFAULT"] = include_inline_default

    @app.get("/inbox")
    def inbox() -> tuple:
        try:
            limit = _parse_int_arg(name="limit", default=5, minimum=1, maximum=1000)
            only_unread = _parse_bool_arg(name="only_unread", default=False)
            items = app.config["EXCHANGE_CLIENT"].list_messages(
                limit=limit, only_unread=only_unread
            )
            return jsonify(items), 200
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except ExchangeClientError:
            LOGGER.exception("Failed to list inbox messages")
            return jsonify({"error": "Unable to fetch inbox"}), 500
        except Exception:
            LOGGER.exception("Unhandled error while listing inbox")
            return jsonify({"error": "Internal server error"}), 500

    @app.get("/message")
    def get_message() -> tuple:
        message_id = request.args.get("id")
        if not message_id:
            return jsonify({"error": "id parameter required"}), 400

        try:
            item = app.config["EXCHANGE_CLIENT"].get_message(message_id)
            item["attachments"] = app.config["ATTACHMENT_STORE"].get_attachments_for_message(
                message_id
            )
            return jsonify(item), 200
        except ExchangeClientError:
            LOGGER.exception("Failed to fetch message", extra={"message_id": message_id})
            return jsonify({"error": "Unable to fetch message"}), 500
        except Exception:
            LOGGER.exception("Unhandled error while fetching message", extra={"message_id": message_id})
            return jsonify({"error": "Internal server error"}), 500

    @app.post("/attachments/sync")
    def sync_attachments() -> tuple:
        try:
            limit = _parse_int_arg(name="limit", default=100, minimum=1, maximum=1000)
            only_unread = _parse_bool_arg(name="only_unread", default=True)
            include_inline = _parse_bool_arg(
                name="include_inline",
                default=app.config["ATTACHMENTS_INCLUDE_INLINE_DEFAULT"],
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        try:
            messages = app.config["EXCHANGE_CLIENT"].list_messages_for_sync(
                limit=limit,
                only_unread=only_unread,
                include_inline=include_inline,
            )
        except ExchangeClientError:
            LOGGER.exception("Attachment sync failed while reading Exchange")
            return jsonify({"error": "Unable to sync attachments"}), 500
        except Exception:
            LOGGER.exception("Unhandled attachment sync read error")
            return jsonify({"error": "Internal server error"}), 500

        summary = {
            "messages_scanned": 0,
            "messages_with_attachments": 0,
            "attachments_saved": 0,
            "attachments_skipped": 0,
            "errors": 0,
        }

        for message in messages:
            summary["messages_scanned"] += 1
            if message.attachments:
                summary["messages_with_attachments"] += 1

            try:
                app.config["ATTACHMENT_STORE"].upsert_message(message)
            except Exception:
                summary["errors"] += 1
                LOGGER.exception("Failed to upsert message metadata", extra={"message_id": message.id})
                continue

            for attachment in message.attachments:
                try:
                    result = app.config["ATTACHMENT_STORE"].save_attachment(message, attachment)
                except Exception:
                    summary["errors"] += 1
                    LOGGER.exception(
                        "Failed to save attachment",
                        extra={"message_id": message.id, "name": attachment.name},
                    )
                    continue

                if result.saved:
                    summary["attachments_saved"] += 1
                    LOGGER.info(
                        "Attachment persisted",
                        extra={
                            "message_id": message.id,
                            "name": attachment.name,
                            "size_bytes": attachment.size_bytes,
                            "sha256": result.sha256,
                            "stored_path": result.stored_path,
                        },
                    )
                else:
                    summary["attachments_skipped"] += 1
                    LOGGER.info(
                        "Attachment skipped",
                        extra={
                            "message_id": message.id,
                            "name": attachment.name,
                            "reason": result.skipped_reason,
                            "sha256": result.sha256,
                        },
                    )

        try:
            app.config["ATTACHMENT_STORE"].set_sync_state(
                "last_sync_at", datetime.now(timezone.utc).isoformat()
            )
        except Exception:
            summary["errors"] += 1
            LOGGER.exception("Failed to persist sync checkpoint")

        LOGGER.info("Attachment sync summary: %s", summary)
        return jsonify(summary), 200

    @app.get("/attachments")
    def list_attachments() -> tuple:
        message_id = request.args.get("message_id")
        try:
            limit = _parse_int_arg(name="limit", default=100, minimum=1, maximum=1000)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        try:
            rows = app.config["ATTACHMENT_STORE"].list_attachments(
                message_id=message_id,
                limit=limit,
            )
            return jsonify(rows), 200
        except Exception:
            LOGGER.exception("Failed to list attachments", extra={"message_id": message_id})
            return jsonify({"error": "Unable to list attachments"}), 500

    return app


PORT = int(os.getenv("PORT", "8765"))
app = None
if os.getenv("WEFA_INIT_ON_IMPORT", "1") == "1":
    try:
        app = create_app()
    except Exception:
        LOGGER.exception("Automatic app initialization failed; use create_app() explicitly.")
        app = None


if __name__ == "__main__":
    runtime_app = create_app()
    runtime_app.run(host="0.0.0.0", port=PORT, debug=False)
