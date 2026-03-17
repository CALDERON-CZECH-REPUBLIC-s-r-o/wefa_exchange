# wefa_exchange

Flask service exposing WEFA Exchange mailbox data over HTTP, including persistent attachment sync/storage.

## Endpoints
- `GET /inbox?limit=5&only_unread=0`
- `GET /message?id=<message_id>`
- `POST /attachments/sync?limit=100&only_unread=1&include_inline=1`
- `GET /attachments?limit=100&message_id=<optional>`

## Attachment storage
- Files: `ATTACHMENTS_ROOT` (default `/data/attachments`)
- SQLite: `ATTACHMENTS_DB_PATH` (default `/data/state/attachments.sqlite3`)
- Inline default: `ATTACHMENTS_INCLUDE_INLINE` (`1` or `0`)
- Max attachment size: `ATTACHMENTS_MAX_BYTES` (default `26214400`)

For persistent host storage, mount:
- `/var/lib/wefa-exchange/attachments:/data/attachments`
- `/var/lib/wefa-exchange/state:/data/state`

## Local run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export EXCHANGE_BACKEND=mock
python app.py
```

## Tests
```bash
pytest -q
```
