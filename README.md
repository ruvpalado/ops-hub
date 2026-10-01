# Ops Hub

A personal ops dashboard: tasks, mail, calendar, and the scheduler that keeps them fresh. Built for Google's 7-day testing-mode token lifecycle.

## Quick start (local)

```bash
# 1. Clone and enter
git clone <this repo>
cd ops-hub

# 2. Bring up the embedded Postgres and write .env
uv run python scripts/dev_db.py

# 3. Install deps
uv sync

# 4. Migrate
uv run python manage.py migrate

# 5. (Optional) Seed a demo workspace — 5 users, 28 tasks, 46 events, 22 threads / 51 messages
#    Only works with OPSHUB_ENV=development (the default).
uv run python manage.py seed_demo

# 6. Run the dev server
uv run python manage.py runserver
```

Open http://127.0.0.1:8000 — you will be redirected to Google OAuth. Sign in with one of the seeded addresses (e.g. `ada@example.test`). The seeded workspace has no tokens, so you will see the reconnect banner and stale-data labels — that is the intended demo state.

## Running the tests

```bash
# Full suite
uv run pytest -q

# Specific areas
uv run pytest tests/test_permissions.py -q          # permissions matrix + smoke
uv run pytest tests/test_task_board.py -q           # Model B task board
uv run pytest tests/test_calendar_sync.py -q        # Calendar sync
uv run pytest tests/test_token_lifecycle.py -q      # Refresh, banner, 7-day expiry
uv run pytest tests/test_normalize.py -q            # Gmail normalization
uv run pytest tests/test_reply.py -q                # Quick reply
uv run pytest tests/test_http.py -q                 # HTTP helpers
```

## CI invariants (kept in sync with .github/workflows/ci.yml)

```bash
# Crypto choke point: no cryptography outside accounts/crypto.py
uv run grep -rEn "^(from cryptography|import cryptography)" --include=*.py \
  --exclude-dir=.venv --exclude-dir=.git . | grep -v "accounts/crypto.py" && echo "LEAK" || echo "CRYPTO_CHOKEPOINT_OK"
# => CRYPTO_CHOKEPOINT_OK

# Templates hold no ORM
uv run grep -rn "\.objects\." --include=*.py ./templates 2>/dev/null && echo "LEAK" || echo "TEMPLATES_CLEAN"
# => TEMPLATES_CLEAN

# Views hold no queries
uv run grep -rEn "objects\.(get|filter|all|create|update|delete|first|last|exists)\(" \
  --include=views.py --exclude-dir=.venv --exclude-dir=.git . && echo "LEAK" || echo "VIEWS_HOLD_NO_QUERIES"
# => VIEWS_HOLD_NO_QUERIES
```

All three must print `*_OK` or `*_CLEAN` or `VIEWS_HOLD_NO_QUERIES`.

## Scheduler

Sync is management commands, not request paths. A hung sync never blocks a page render.

```bash
# Run both syncs once (for all active sources)
uv run python manage.py sync_gmail --all
uv run python manage.py sync_calendar --all
```

### Production scheduler

Two equivalent definitions are provided:

- **Procfile** — for Heroku/Fly/Render: `web` (gunicorn) + `scheduler` (5-min loop)
- **deploy/scheduler.yml** — Kubernetes CronJob definitions (staggered, `concurrencyPolicy: Forbid`)

The scheduler query uses the index `source_status_next_attempt_idx` on `core_source(status, next_attempt_at)`.

## Key commands

| Command | Purpose |
|---------|---------|
| `manage.py sync_gmail` | Gmail history sync (supports `--all`, `--dry-run`) |
| `manage.py sync_calendar` | Calendar sync (supports `--all`, `--dry-run`) |
| `manage.py seed_demo` | Seed 5 users, 28 tasks, 46 events, 22 threads / 51 messages (dev only) |
| `manage.py set_token_key --write-env` | Generate and write `OPSHUB_TOKEN_KEY` to `.env` |

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | yes | Postgres connection (unix socket for dev_db.py) |
| `OPSHUB_TOKEN_KEY` | yes | 32 random bytes, base64. Generate with `manage.py set_token_key --write-env` |
| `OPSHUB_SECRET_KEY` | yes | Django signing key. `manage.py set_token_key` writes both. |
| `OPSHUB_ENV` | no | `development` (default) \| `test` \| `production`. Gates `seed_demo`. |
| `GOOGLE_CLIENT_ID` | yes (prod) | OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | yes (prod) | OAuth client secret |
| `GOOGLE_REDIRECT_URI` | yes (prod) | OAuth redirect URI |

## Project layout (relevant parts)

```
.
├── accounts/           # User model, Google OAuth, token storage, refresh
├── audit/              # Append-only audit trail (triggers block UPDATE/DELETE)
├── calendar_app/       # CalendarEvent, Google sync, views, services
├── core/               # Source sync state, scheduler, permissions, crypto
├── mail/               # Message, Gmail sync, normalize, reply, views, services
├── tasks/              # Task board (Model B permissions), views, services
├── opshub/             # Django settings, urls, wsgi, healthz
├── deploy/             # scheduler.yml (K8s CronJob)
├── scripts/            # dev_db.py (embedded pg + .env bootstrap)
├── templates/          # Server-rendered HTML (no SPA)
└── tests/              # All test modules
```

## Go-live gates

See `docs/go-live-gates.md` for G1–G5. The app runs in Google's **Testing mode** (7-day token expiry) until G1 is flipped.

## License

MIT