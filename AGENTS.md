# AGENTS.md

Operations guide for the **wefa_exchange** Flask service. This app fronts a WEFA Exchange mailbox (no IMAP/POP3) and is deployed on the Ubuntu host behind Traefik and Docker.

## Purpose
- Serve a lightweight HTTP API to list and read mailbox messages via Exchange/EWS (or mock backend).
- Intended to run as container `wefa-exchange` on Docker network `wefa_net`, routed by Traefik (see host `/opt/traefik` stack).

## Code layout
- `app.py` — Flask app factory and routes (`/inbox`, `/message`, `/attachments/sync`, `/attachments`).
- `exchange_client.py` — live EWS client and mock backend with normalized message/attachment payloads.
- `attachments_store.py` — SQLite + filesystem persistence, filename sanitization, atomic attachment writes.
- `tests/` — pytest unit/integration coverage for storage safety and attachment APIs.
- `Dockerfile` — Python 3.12 image installing `requirements.txt`, default envs for Exchange and attachment storage.
- `.env` (not committed) — loaded automatically via `python-dotenv` and injected into the container via compose `env_file`.

## API
- `GET /inbox?limit=5&only_unread=0` → JSON list of message summaries; `only_unread=1` filters unread; `limit` caps results.
- `GET /message?id=<message_id>` → full message payload; `id` is required. Response now includes `attachments` metadata from local store.
- `POST /attachments/sync?limit=100&only_unread=1&include_inline=1` → scans mailbox messages and saves attachments idempotently.
- `GET /attachments?limit=100&message_id=<optional>` → lists stored attachment metadata only (no file bytes).
- Errors return `{"error": "<reason>"}` with 4xx/5xx.

## Configuration (env vars)
- `EXCHANGE_BACKEND`: `mock` (default) or `live`.
- `EXCHANGE_URL`: EWS endpoint (e.g., `https://owa.wefa.com/EWS/Exchange.asmx`).
- `EXCHANGE_USER`: AD username (e.g., `WEFASINGEN\\obchodcz`).
- `EXCHANGE_PASSWORD`: password for the above user.
- `MAILBOX`: target mailbox SMTP address.
- `PORT`: Flask listen port; defaults to `8765` in code, overridden to `8000` in Dockerfile/compose.
- `ATTACHMENTS_ROOT`: root directory for attachment files (default `/data/attachments`).
- `ATTACHMENTS_DB_PATH`: SQLite path for sync state and metadata (default `/data/state/attachments.sqlite3`).
- `ATTACHMENTS_INCLUDE_INLINE`: default inline attachment behavior for sync endpoint (`1` include, `0` exclude).
- `ATTACHMENTS_MAX_BYTES`: per-attachment hard limit in bytes (default `26214400` / 25 MB).
- TLS: live client disables certificate verification via `NoVerifyHTTPAdapter`; keep traffic on trusted network.

## Local run (direct)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export EXCHANGE_BACKEND=mock  # or live + credentials
python app.py  # listens on 0.0.0.0:${PORT:-8765}
```

## Docker / Traefik deployment
- Host stack lives in `/opt/traefik` (see global `/home/calderon/AGENTS.md`).
- Network: attach to external `wefa_net` (172.31.251.0/24). Avoid other networks unless justified.
- Traefik routes: `Host("exchange.localhost")` → `wefa-exchange:8000` via `/opt/traefik/dynamic.yml`; Traefik binds `127.0.0.1:80/8080`.
- Host storage (persistent across container recreation):
  - `/var/lib/wefa-exchange/attachments`
  - `/var/lib/wefa-exchange/state`
- Required compose mounts:
  - `/var/lib/wefa-exchange/attachments:/data/attachments`
  - `/var/lib/wefa-exchange/state:/data/state`
- Build/run with the Traefik compose file:
  ```bash
  cd /opt/traefik
  sudo install -d -m 750 /var/lib/wefa-exchange/attachments /var/lib/wefa-exchange/state
  docker compose config
  docker compose up -d --build wefa-exchange
  ```
- Access (from a developer machine): `ssh -L 8080:127.0.0.1:80 calderon@10.0.150.248` and set hosts `127.0.0.1 exchange.localhost`; hit `http://exchange.localhost:8080/inbox`.

## Operations
- Logs: `docker logs wefa-exchange`.
- Restart: `docker restart wefa-exchange` (or `docker compose restart wefa-exchange` in `/opt/traefik`).
- Switch to live mailbox: set `EXCHANGE_BACKEND=live` and provide `EXCHANGE_URL`, `EXCHANGE_USER`, `EXCHANGE_PASSWORD`, `MAILBOX`; rebuild/restart container.
- Health check: `curl -s http://exchange.localhost:8080/inbox` (through SSH tunnel as above).
- Run attachment sync: `curl -s -X POST "http://exchange.localhost:8080/attachments/sync?limit=100&only_unread=1&include_inline=1"`.
- Inspect stored metadata: `curl -s "http://exchange.localhost:8080/attachments?limit=20"`.

## Safety notes
- Keep container on `wefa_net`; do not bind to `0.0.0.0` host ports—Traefik already binds to localhost.
- Maintain `/opt/traefik/dynamic.yml` in sync with any port/host changes; the Docker provider is not enabled.
- Treat stored credentials as secrets; prefer `.env` or compose overrides, not baked into images.
- Keep `/var/lib/wefa-exchange/attachments` and `/var/lib/wefa-exchange/state` in backup scope.
