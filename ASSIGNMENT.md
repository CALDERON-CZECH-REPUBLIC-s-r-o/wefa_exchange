# ASSIGNMENT: Attachment Retrieval and Server Storage for `wefa_exchange`

## 1) Current Status (As-Is)
- The current service does **not** retrieve, serialize, or store attachments.
- `GET /inbox` returns only message summary fields.
- `GET /message` returns body text only, without attachment metadata or files.
- There is no attachment persistence layer, no sync state, and no attachment-focused logs.

Code references:
- `app.py:46` `_serialize_message_full()` only adds `"body"`.
- `app.py:101` `get_message()` returns serialized message without attachments.

## 2) Goal (To-Be)
Implement production-grade support to:
1. Discover attachments for mailbox messages.
2. Download and store **all** attachments found (including inline if configured).
3. Persist attachment metadata and sync state on the Ubuntu host.
4. Expose safe API endpoints to trigger sync and inspect stored results.

## 3) Constraints
- Keep current mailbox features working (`/inbox`, `/message`).
- Preserve Traefik + Docker deployment model from `AGENTS.md`.
- Do not expose new host ports; route through existing Traefik path.
- Store data on host filesystem (persistent across container recreation).
- Secrets remain in `.env` / compose env injection, not in code.

## 4) Target Design

### 4.1 Storage Design (Host + Container)
- Host paths:
  - `/var/lib/wefa-exchange/attachments` (binary files)
  - `/var/lib/wefa-exchange/state` (SQLite state DB)
- Container mounts:
  - `/var/lib/wefa-exchange/attachments:/data/attachments`
  - `/var/lib/wefa-exchange/state:/data/state`
- New env vars:
  - `ATTACHMENTS_ROOT=/data/attachments`
  - `ATTACHMENTS_DB_PATH=/data/state/attachments.sqlite3`
  - `ATTACHMENTS_INCLUDE_INLINE=1` (default include all)
  - `ATTACHMENTS_MAX_BYTES=26214400` (25MB safety limit per file; configurable)

### 4.2 Data Model (SQLite)
Create SQLite DB at `ATTACHMENTS_DB_PATH` with:
- `messages`:
  - `message_id` TEXT PK
  - `subject`, `sender`, `datetime_received`, `is_read`
  - `last_seen_at` timestamp
- `attachments`:
  - `id` INTEGER PK
  - `message_id` TEXT FK -> messages
  - `attachment_id` TEXT NULL (Exchange may not always provide stable ID)
  - `name` TEXT
  - `content_type` TEXT
  - `size_bytes` INTEGER
  - `is_inline` INTEGER
  - `sha256` TEXT
  - `stored_path` TEXT (absolute path in container, under `/data/attachments`)
  - `stored_at` timestamp
  - UNIQUE (`message_id`, `sha256`, `name`)
- `sync_state`:
  - `key` TEXT PK
  - `value` TEXT
  - Use for cursor/checkpoint (e.g. last sync timestamp).

### 4.3 File Naming and Safety
- Never trust attachment names.
- Sanitize filenames:
  - strip path separators, control chars, leading dots
  - enforce max filename length
- Save via atomic write:
  - write temp file in target dir, `fsync`, then `rename`
- Suggested path:
  - `/data/attachments/<mailbox>/<YYYY>/<MM>/<message_id>/<sha256>_<safe_name>`
- Reject path traversal and log blocked files.

### 4.4 API Design
Keep existing endpoints backward-compatible; add:

1. `POST /attachments/sync`
- Query params:
  - `limit` (default `100`)
  - `only_unread` (`0|1`, default `1`)
  - `include_inline` (`0|1`, default from env)
- Behavior:
  - fetch messages ordered by newest
  - for each message, download all file attachments
  - store files + metadata idempotently
- Response:
  - counts (`messages_scanned`, `messages_with_attachments`, `attachments_saved`, `attachments_skipped`, `errors`)

2. `GET /attachments`
- Query params:
  - `message_id` (optional)
  - `limit` (default `100`)
- Response:
  - metadata records (no raw file bytes)

3. Extend `GET /message?id=...`
- Add `attachments` metadata array (name, size, content_type, is_inline, sha256, stored_path if downloaded).

## 5) Implementation Tasks for AI Agent

1. Refactor structure:
- Split `app.py` into focused modules:
  - `exchange_client.py` (EWS client + serializers)
  - `attachments_store.py` (filesystem + SQLite)
  - `api.py` or keep Flask routes in `app.py` with clear separation.

2. Implement attachment extraction in live backend:
- For each message item, iterate `item.attachments`.
- Handle at least `FileAttachment`.
- Skip unsupported attachment classes with warning logs.

3. Implement persistent storage:
- Initialize SQLite schema on app start.
- Save message and attachment metadata transactionally.
- Implement idempotency (same attachment not duplicated).

4. Implement new endpoints:
- `POST /attachments/sync`
- `GET /attachments`
- Extend `/message` response with attachment metadata.

5. Add robust logging:
- Configure Python `logging` with INFO default.
- Log each sync run summary.
- Log per-attachment save outcome with message_id, safe filename, size, sha256.
- Log full exception details server-side while keeping client errors generic.

6. Add tests:
- Unit:
  - filename sanitization
  - path traversal prevention
  - idempotent duplicate suppression
- Integration (mock backend):
  - message with multiple attachments
  - inline + non-inline attachment handling
  - API contract for `/attachments/sync` and `/attachments`

7. Update deployment artifacts:
- Update compose service for volume mounts and new env vars.
- Document host directory provisioning and permissions.

8. Documentation updates:
- Update `AGENTS.md` and/or `README.md` with:
  - new endpoints
  - storage layout
  - operational commands (sync, inspect, cleanup).

## 6) Operational Best Practices
- Principle of least privilege on host directories:
  - owner-only write where possible
  - avoid world-readable attachments by default
- Backup policy:
  - include `/var/lib/wefa-exchange/state` and `/var/lib/wefa-exchange/attachments`
- Retention policy:
  - optional cleanup job by age or total size threshold
- Observability:
  - use `docker logs wefa-exchange` for sync outcomes
  - expose health endpoint extension if needed (optional).

## 7) Acceptance Criteria (Definition of Done)
- `POST /attachments/sync` returns success and non-zero counts when mailbox has attachments.
- Files are persisted on host under `/var/lib/wefa-exchange/attachments`.
- Re-running sync is idempotent (no duplicate files/DB rows for same attachment).
- `/message` includes attachment metadata.
- `/attachments` lists stored attachments and supports filtering by `message_id`.
- Error cases are logged with actionable detail; API returns safe error messages.
- Existing `/inbox` and `/message` behavior remains backward-compatible.

## 8) Suggested Delivery Sequence
1. Implement storage + schema + sanitization utilities.
2. Implement EWS attachment extraction and save flow.
3. Expose `/attachments/sync` and `/attachments`.
4. Extend `/message` payload.
5. Add tests.
6. Roll out via Traefik compose rebuild and run smoke tests.
