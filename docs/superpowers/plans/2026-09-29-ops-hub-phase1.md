# Ops Hub — Phase 1 Implementation Plan

- **Date:** 2026-09-29
- **Spec:** [`../specs/2026-09-29-ops-hub-design.md`](../specs/2026-09-29-ops-hub-design.md) — the source of truth. This plan does not restate it; it sequences it.
- **Stack:** Django 5 + Postgres + Gunicorn, server-rendered, pytest.
- **Status:** not started. No code written. No repo initialized.

## How to read this plan

26 steps, in dependency order. Each one is independently verifiable: it names the files it touches, the exact command that proves it, and what that command must print. A step is done when its command prints its expected output — not when the code looks finished.

**OAuth comes first, on purpose.** Steps 4–13 are the token lifecycle, before any feature depends on it. The spec makes Testing mode the design target, which means the 7-day refresh-token expiry is a permanent weekly operating state, not an edge case discovered at launch. Every other decision in this plan is reversible; a wrong token design is not, because it is written into the schema.

**Commands assume** the repo root and a running Postgres (§ steps 1–2). `uv` is the only tool required beyond Python 3.12.

**Two invariants every step must preserve:**

1. No plaintext token exists outside `accounts/crypto.py`. A CI grep in step 26 fails the build if another module imports `cryptography`.
2. No permission check lives in a view. Views hold no ORM queries; services take the actor. A CI grep in step 26 fails the build on `objects.` outside the services layer.

---

## Phase 0 — Foundations

### Step 1 — Repository, project skeleton, dependency lock

**What:** A runnable Django project with a committed lockfile, env-driven settings, a health endpoint, and a local Postgres. Nothing domain-specific yet.

**Files:** `pyproject.toml`, `uv.lock`, `.gitignore`, `.env.example`, `Dockerfile`, `docker-compose.yml`, `manage.py`, `opshub/settings.py`, `opshub/urls.py`, `opshub/views.py`, `.github/workflows/ci.yml`

Dependencies: `django`, `psycopg[binary]`, `gunicorn`, `cryptography`, `google-api-python-client`, `google-auth`, `zoneinfo` is stdlib. Dev: `pytest`, `pytest-django`, `pytest-randomly`.

`.env.example` holds **names only** — `DATABASE_URL`, `OPSHUB_TOKEN_KEY`, `OPSHUB_ENV`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `OPSHUB_ALLOW_SIGNUP`, `OPSHUB_SIGNUP_INVITE_ONLY`, `GOOGLE_REDIRECT_URI`. No values, no placeholders that look like values.

**Proof:**
```bash
uv sync && cp .env.example .env && \
  docker compose up -d db && \
  uv run python manage.py check && \
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/healthz
```

**Expected:** `System check identified no issues` and `200`.

---

### Step 2 — First migration and a bootable database

**What:** The database exists, is migrated, and the app boots against it. This is the point at which every later migration is known to work.

**Files:** `accounts/migrations/0001_initial.py` (empty at this point), `.github/workflows/ci.yml` (adds the two greps from the invariants above, as `|| exit 1` steps)

**Proof:**
```bash
uv run python manage.py migrate && \
  uv run python manage.py showmigrations | head -5
```

**Expected:** `Applying accounts.0001_initial... OK` and a table of `X yes` rows.

---

## Phase 1 — Token lifecycle (highest risk, built first)

### Step 3 — Log redaction

**What:** A logging filter that drops token-shaped values from every record, installed on the root logger. Built before any code can log, so there is no window in which a token can escape.

**Files:** `core/redaction.py`, `opshub/settings.py` (`LOGGING`), `tests/test_token_lifecycle.py` (first tests in the file)

The filter redacts by key name (`access_token`, `refresh_token`, `id_token`, `client_secret`, `authorization`) and by value shape (`1//` prefix, `ya29.` prefix, and any `OPSHUB_TOKEN_KEY` value it can compare against).

**Proof:**
```bash
uv run pytest tests/test_token_lifecycle.py -k redaction -q
```

**Expected:** a line containing `4 passed`. The tests assert a synthetic record with a real-looking refresh token emits no substring of that token, at DEBUG and at ERROR, as a top-level field and nested three levels deep.

---

### Step 4 — Token encryption

**What:** AES-256-GCM encrypt/decrypt, and a key loader that reads `OPSHUB_TOKEN_KEY` from the environment or fails loudly.

**Files:** `accounts/crypto.py`, `accounts/management/commands/set_token_key.py`, `tests/test_token_lifecycle.py`

Behavior fixed here, because these are the properties that decide whether a key rotation is survivable:

- The nonce is 12 random bytes, prepended to the ciphertext, never reused.
- A wrong key raises. It never returns an empty string, because an empty string that looks like a valid decrypt is how a broken key silently disconnects every user at once.
- Decryption failures log the error class and nothing else.
- Key rotation re-encrypts in place via a management command, and is testable.

**Proof:**
```bash
OPSHUB_TOKEN_KEY=$(openssl rand -base64 32) uv run pytest tests/test_token_lifecycle.py -k "crypto or tamper or key" -q
```

**Expected:** a line containing `7 passed`, including one test that flips a single ciphertext byte and asserts an exception, and one that loads a different key and asserts an exception rather than a value.

---

### Step 5 — `app_user`, including the two columns the amended spec required

**What:** The user table, with `google_sub` **nullable** and a partial unique index, `email` unique, `timezone`, and `refresh_token_obtained_at`.

**Files:** `accounts/models.py`, `accounts/migrations/0002_app_user.py`

Three of these are not obvious and each has a reason recorded in the spec:

- `google_sub` is nullable because invite-only (go-live gate G2) needs a pre-created row with no Google identity yet. Making it `NOT NULL` makes gate G2 a schema migration under time pressure.
- The unique constraint on `google_sub` is **partial** (`WHERE google_sub IS NOT NULL`), because Postgres treats NULLs as distinct in a unique index — a plain unique index would permit any number of unclaimed placeholder rows, which is correct, and the partial form says so explicitly.
- `refresh_token_obtained_at` exists because Google never reports when a refresh token expires. Without a locally recorded clock, the app cannot distinguish "Testing-mode 7-day expiry" from "user revoked access" — and those two warrant different banners and different audit entries.

**Proof:**
```bash
uv run python manage.py makemigrations --check --dry-run && \
  uv run python manage.py shell -c "
from django.db import connection
c = connection.cursor()
c.execute(\"select indexname, indexdef from pg_indexes where tablename='app_user' order by indexname\")
[print(n) for n, _ in c.fetchall()]" && \
  uv run python manage.py shell -c "
from accounts.models import AppUser
print(AppUser._meta.get_field('google_sub').null, AppUser._meta.get_field('email').unique)"
```

**Expected:** `No changes detected`, then three index names including one matching `app_user_google_sub_` and one matching `app_user_email_`, then `True True`.

---

### Step 6 — `source`, with the enumerated error codes

**What:** One row per integration per user, carrying sync bookkeeping and health.

**Files:** `calendar_app/models.py`, `mail/models.py` (or a shared `core/models.py` — whichever the migration graph ends up cleanest with), `accounts/migrations/0003_source.py`

`last_error_code` is a `CHECK` constraint over a fixed set, not free text: `invalid_grant`, `no_token`, `insufficient_scope`, `sync_token_expired`, `rate_limited`, `upstream_5xx`, `network`. The admin dashboard has to tell "expired by Testing mode" apart from "revoked by the user" without parsing prose, and a check constraint is what makes that reliable.

Indexes: `UNIQUE (user_id, kind)` and `INDEX (status, next_attempt_at)`. The second is the scheduler's only query.

**Proof:**
```bash
uv run python manage.py shell -c "
from django.db import connection, IntegrityError, transaction
from accounts.models import AppUser
from core.models import Source
u = AppUser.objects.create(email='idx@example.test', google_sub='idx-1', role='member')
s = Source.objects.create(user=u, kind='gmail', external_id=u.email)
print('status', s.status, 'error', s.last_error_code)
try:
    with transaction.atomic():
        Source.objects.create(user=u, kind='gmail', external_id='dupe')
except IntegrityError:
    print('unique_ok')
print(Source._meta.get_field('status').default)
" 2>&1 | tail -3
```

**Expected:** `status active error None`, then `unique_ok`, then `active`.

---

### Step 7 — Audit log with database-level immutability

**What:** An append-only `audit_log` table and the single writer every other app calls.

**Files:** `audit/models.py`, `audit/writer.py`, `core/db_guards.py`, `accounts/migrations/0004_audit_log.py`, `tests/test_permissions.py` (the audit rows)

Immutability is a database rule, not a convention: `BEFORE UPDATE OR DELETE` triggers that `RAISE EXCEPTION`. A rule that lives only in application code is a rule that the next `manage.py shell` session will break.

`audit.record(actor, action, target_type, target_id, metadata)` is the only call site. The writer drops any `metadata` key whose name appears in the redaction list — a caller passing a token in metadata must not be able to.

**Proof:**
```bash
uv run pytest tests/test_permissions.py -k audit -q && \
  uv run python manage.py shell -c "
from django.db import connection
c = connection.cursor()
c.execute(\"select tgname from pg_trigger where tgname like 'audit_log_immutable%'\")
print([n for (n,) in c.fetchall()])"
```

**Expected:** a line containing `2 passed`, then a list containing two trigger names.

---

## Phase 2 — OAuth end to end

### Step 8 — Authorization request: `state`, PKCE, scopes

**What:** `GET /auth/google` generates `state` (32 CSPRNG bytes) and a `code_verifier`, stores both in the session with a created-at timestamp, and redirects to Google's consent endpoint with `access_type=offline`, `prompt=consent`, `include_granted_scopes=false`, and exactly three scopes: `gmail.readonly`, `gmail.send`, `calendar.readonly`.

**Files:** `accounts/google_client.py`, `accounts/views.py`, `opshub/urls.py`, `tests/test_token_lifecycle.py`

`prompt=consent` is what forces Google to reissue a refresh token on reconnect, which is the only reason the reconnect flow can restore a dead user's access. Without it a reconnect silently succeeds and does nothing.

**Proof:**
```bash
uv run pytest tests/test_token_lifecycle.py -k "state or pkce or scope" -q
```

**Expected:** a line containing `5 passed`. One test asserts the redirect URL contains `access_type=offline` and `prompt=consent`; one asserts the redirect URL contains `gmail.readonly`, `gmail.send`, and `calendar.readonly`; one asserts `calendar` (the read/write variant) is **absent**; one asserts two requests produce different `state` values; one asserts a 10-minute-old session value is rejected.

---

### Step 9 — Callback: state check, code exchange, identity

**What:** `GET /auth/google/callback` compares `state` with a constant-time compare, deletes both session values, exchanges the code with PKCE, and reads identity from `userinfo`.

**Files:** `accounts/google_client.py`, `accounts/views.py`, `tests/test_token_lifecycle.py`

`email` is accepted only from the **verified** claim. An unverified email is rejected and logged as `auth.unverified_email` — accepting it would let anyone claim a role by asserting an address they do not control.

The token endpoint is faked in every test. No test in this project makes a network call.

**Proof:**
```bash
uv run pytest tests/test_token_lifecycle.py -k "callback or unverified or state_mismatch" -q
```

**Expected:** a line containing `6 passed`, including one asserting a state mismatch yields 400 and that **no** code exchange was attempted.

---

### Step 10 — Identity upsert and the invite-or-claim decision

**What:** Upsert on `google_sub`, then claim a pre-created row, then apply the signup posture.

**Files:** `accounts/views.py`, `accounts/services.py`, `tests/test_permissions.py` (row 1), `tests/test_token_lifecycle.py`

The order matters and is the reason this is one step rather than two:

1. Look for an `app_user` with `google_sub IS NULL` whose lowercased `email` matches the claim. If found, write `google_sub` onto **that row** and keep its `role`. This is how an admin-granted role and a pre-created invite seat survive to a user's first sign-in.
2. If `OPSHUB_SIGNUP_INVITE_ONLY` is off (phase-1 posture) and no row matched, create a new `member`.
3. If it is on and no row matched, reject with a 403 page naming the address that tried to sign in, so an admin can see who was turned away and pre-create the row.

Both branches ship in phase 1. Gate G2 flips the flag; it does not write code.

**Proof:**
```bash
OPSHUB_SIGNUP_INVITE_ONLY=0 uv run pytest tests/test_permissions.py -k "signup" -q && \
  OPSHUB_SIGNUP_INVITE_ONLY=1 uv run pytest tests/test_permissions.py -k "signup" -q
```

**Expected:** two lines each containing `4 passed`. The second run asserts: unknown email → 403 and no user row created; pre-created unclaimed row → same user id after claim, role preserved.

---

### Step 11 — Encrypted token storage

**What:** Tokens land on the user row encrypted, `refresh_token_obtained_at` is set only when a new refresh token actually arrives, and `source` rows are upserted `active`.

**Files:** `accounts/services.py`, `tests/test_token_lifecycle.py`

A callback response with no `refresh_token` (Google omits it when the existing grant is still valid) must preserve the stored token **and** leave `refresh_token_obtained_at` untouched. Resetting the clock on every login would make the 7-day prediction permanently wrong.

**Proof:**
```bash
uv run pytest tests/test_token_lifecycle.py -k "store or preserves or obtained_at" -q && \
  uv run python manage.py shell -c "
from accounts.models import AppUser
f = [f.name for f in AppUser._meta.get_fields() if 'token' in f.name]
print(f)"
```

**Expected:** a line containing `5 passed`, then `['refresh_token', 'access_token', 'refresh_token_obtained_at', 'token_expires_at']`.

---

### Step 12 — Refresh, `invalid_grant`, and transient failure

**What:** The three refresh outcomes, each with a different consequence.

**Files:** `accounts/google_client.py`, `tests/test_token_lifecycle.py`

| Outcome | Effect |
| --- | --- |
| Token comfortably valid | **No call at all.** A no-op refresh is a wasted round-trip and a wasted quota unit. |
| `expires_at` under 60s away | Refresh, update `token_expires_at` and `access_token`, leave `refresh_token` alone. |
| `invalid_grant` | `needs_reauth` + `disconnected_at` + `last_error_code='invalid_grant'`. **Task and message data is preserved.** Never auto-retried. |
| `5xx` / network | `next_attempt_at = now() + 15m` after 2s and 8s. **Does not disconnect.** A flaky network must not cost a user their sync. |

**Proof:**
```bash
uv run pytest tests/test_token_lifecycle.py -k "refresh" -q
```

**Expected:** a line containing `9 passed`, including one asserting task rows are byte-identical before and after an `invalid_grant`.

---

### Step 13 — Testing mode: the 7-day expiry as an operating state

**What:** Everything the amended spec turned into a permanent condition. **This step is the reason the whole plan is sequenced this way.**

**Files:** `accounts/google_client.py` (`sweep_expiring_tokens`), `accounts/views.py` (`/reconnect`), `core/redaction.py` (unchanged), `templates/partials/reconnect_banner.html`, `calendar_app/management/commands/sync_calendar.py` (the `status='active'` filter), `tests/test_token_lifecycle.py`

Five behaviors, each tested:

1. `sweep_expiring_tokens` finds sources whose `refresh_token_obtained_at` is older than 7 days, still `active`, and flags them — the day-5 proactive banner.
2. An `invalid_grant` on a token older than 7 days sets `needs_reauth` and produces a banner that **names the 7-day cause** rather than a generic error, because the two causes are distinguishable and the user can act on one of them.
3. A token younger than 7 days returning `invalid_grant` is reported as a **revocation**, not an expiry.
4. The scheduler query filters `status='active'`, so a disconnected user costs zero API calls. This is asserted against the database, not against the source text.
5. `/reconnect` completes the full flow and restores `active` with a fresh `refresh_token_obtained_at`.

**Proof:**
```bash
uv run pytest tests/test_token_lifecycle.py -k "testing or seven_day or expiring or reconnect or skip" -q
```

**Expected:** a line containing `11 passed`. The suite includes a test that names each of the five behaviors above in its test name, so a failure names the missing behavior rather than an opaque function.

---

## Phase 3 — Permissions and the task board

### Step 14 — The permission choke point

**What:** Role resolution and the two decorators (`login_required`, `admin_required`) that every view uses, so a view cannot accidentally skip them.

**Files:** `core/permissions.py`, `tests/test_permissions.py` (row-parity scaffolding)

A member requesting an admin route gets **403, not 302** — a redirect to a login page for an already-authenticated user leaks the existence of the route.

**Proof:**
```bash
uv run pytest tests/test_permissions.py -k "decorator or role" -q
```

**Expected:** a line containing `4 passed`.

---

### Step 15 — `task` table and the Model B predicate

**What:** The task model, its indexes, and the single function that decides who may edit what.

**Files:** `tasks/models.py`, `tasks/services.py`, `tasks/migrations/0005_task.py`, `tests/test_permissions.py`

Model B, in one function, enforced in the SQL `WHERE` clause rather than by a read-then-check:

- admin → may edit any task
- assignee → may edit their own, including reassigning it
- unassigned → any member may claim it by assigning it to themselves
- unassigned → a member may **not** set it to a third party's id (row 8a)
- anyone who is not the assignee and not an admin → 403
- delete → admin only

**Files matter here:** putting this in the queryset means a bug in a view cannot widen access, because there is no second code path.

**Proof:**
```bash
uv run pytest tests/test_permissions.py -k "task_edit" -q && \
  uv run python manage.py shell -c "
from django.db import connection
c = connection.cursor()
c.execute(\"select indexname from pg_indexes where tablename='task' order by 1\")
print([n for (n,) in c.fetchall()])"
```

**Expected:** a line containing `15 passed` — the full §10 matrix, including the case that catches a read-then-check implementation (a member trying to reassign a colleague's task to themselves). Then three index names: the `UNIQUE (id)`, one on `(assignee_id, status)`, one on `(status, due_at)`.

---

### Step 16 — Task board pages

**What:** List, detail, create, edit, claim, admin delete, with pagination.

**Files:** `tasks/views.py`, `tasks/services.py`, `templates/tasks/*.html`

Views contain no ORM queries. The list is paginated at 50; there is no unbounded result set on any path. The index `(assignee_id, status)` exists for the "my tasks" default view and the index `(status, due_at)` for the board's due-soon column — both are real query patterns, not guesses.

**Proof:**
```bash
uv run pytest tests/test_permissions.py -k "board" -q && \
  uv run python manage.py shell -c "
from tasks.services import board_page
print(board_page(owner_id=1, page=9999).num_pages)"
```

**Expected:** a line containing `3 passed`, then an integer ≥ 1 (an out-of-range page clamps rather than raising).

---

## Phase 4 — Availability (the algorithm, built pure and tested first)

### Step 17 — Merge busy blocks and detect overlaps

**What:** `merge_busy_blocks(blocks, window)` and `find_overlaps(blocks, threshold_minutes)`, as pure functions with no Django import.

**Files:** `calendar_app/availability.py`, `tests/test_availability.py`

**Proof:**
```bash
uv run pytest tests/test_availability.py -k "merge or overlap" -q
```

**Expected:** a line containing `8 passed` — adjacent, three-deep chain, nested, clipped at window edges, out-of-window, plus the 5-minute overlap threshold with a 3-minute case that must **not** flag.

---

### Step 18 — `next_free_slot`, the full boundary table

**What:** The §5.4 algorithm, including the worked example verbatim as a test.

**Files:** `calendar_app/availability.py`, `tests/test_availability.py`

`duration_minutes` is validated against a fixed enum (30/45/60/90), not free text. Windows are capped at 31 days. Candidates are at most 5, on a 15-minute grid aligned to the **local clock**. A gap exactly equal to `duration_minutes` is emitted; one minute short is not. The no-slot case returns `[]` **with a reason** — a silent empty list is the failure mode that makes people stop trusting a scheduling tool.

The file must import without `django.setup()`, which is what makes step 18 verifiable in milliseconds.

**Proof:**
```bash
uv run pytest tests/test_availability.py -q
```

**Expected:** a line containing `20 passed` — the whole §10 table, including the worked example asserting exactly `[14:00Z, 18:00Z, 19:00Z, 20:00Z]`, both DST directions, and the free window *before* the first busy block (which catches an implementation that starts its cursor at the first conflict instead of the window start).

---

## Phase 5 — Sync

### Step 19 — Calendar sync

**What:** `sync_calendar` pulls `events.list` with the stored `syncToken`, upserts by `(owner_id, google_event_id)`, and advances the cursor.

**Files:** `calendar_app/google_sync.py`, `calendar_app/management/commands/sync_calendar.py`, `calendar_app/models.py`, `accounts/migrations/0006_calendar_event.py`

`freebusy.query` is **not** called. `events.list` under `calendar.readonly` already returns `transparency`, which is where `is_busy` comes from, and one call per sync is cheaper than two. `syncToken` expiry (`410 Gone`) is caught, the cursor is cleared, and a full resync runs — that is a normal event, not an error state.

**Proof:**
```bash
uv run pytest tests/test_normalize.py -k "calendar or sync_token" -q && \
  uv run python manage.py shell -c "
from calendar_app.models import CalendarEvent
print(CalendarEvent._meta.get_field('owner_id').db_index)"
```

**Expected:** a line containing `5 passed`, then `True`.

---

### Step 20 — Gmail normalization

**What:** `normalize.py` maps a Gmail thread to `message` rows and maps unread state.

**Files:** `mail/normalize.py`, `mail/models.py`, `accounts/migrations/0007_message.py`, `tests/test_normalize.py`

`body_html` is stored but **never rendered** in phase 1 — it is a stored XSS vector until someone renders it, and the mitigation (sandboxed iframe) should not be half-built.

**Proof:**
```bash
uv run pytest tests/test_normalize.py -q
```

**Expected:** a line containing `11 passed`, including the RFC 5322 chain case that makes the §6.3 reply builder produce correct `In-Reply-To` and `References` against seeded data.

---

### Step 21 — Gmail sync and quota behavior

**What:** `sync_gmail` walks `history.list` from the stored `historyId`, fetches changed messages in batches, and upserts.

**Files:** `mail/google_sync.py`, `mail/management/commands/sync_gmail.py`, `tests/test_normalize.py`

Ceiling accepted: 100-message batches, 30-day window on cold start, ~50 users on one domain. Beyond that it is a second OAuth client or a second Workspace domain — a System Architect decision, not a code change.

**Proof:**
```bash
uv run pytest tests/test_normalize.py -k "history or batch or quota" -q
```

**Expected:** a line containing `4 passed`, including a `rate_limited` case that sets `next_attempt_at` and does **not** set `status='error'`.

---

### Step 22 — Quick reply with idempotency

**What:** The reply path: ownership check, MIME construction, idempotency, send, audit.

**Files:** `mail/views.py`, `mail/services.py`, `tests/test_normalize.py`

A thread owned by someone else returns **404, not 403** — the response code must not confirm that another person's mailbox exists. The idempotency key is checked before sending, which makes double-clicks, back-button resubmits, and retry-after-timeout all safe. This is the one place in the app where a duplicate email is genuinely harmful, so it is the one place the check is unconditional.

**Proof:**
```bash
uv run pytest tests/test_normalize.py -k "reply or idempot" -q
```

**Expected:** a line containing `7 passed`, including a double-submit case asserting **one** outbound row and one `messages.send` call.

---

## Phase 6 — Seed, UI, and the run

### Step 23 — `seed_demo`

**What:** 5 users, 28 tasks, 46 calendar events across 14 days, 22 threads / 51 messages. Idempotent, and refuses to run outside development.

**Files:** `core/management/commands/seed_demo.py`, `tests/test_permissions.py` (one seed-shape test)

Two properties are load-bearing:

- Seeded `source` rows are **`disconnected` with `last_error_code='no_token'`**, not `active`. A source with no token is not connected, the scheduler skips it (exactly as it skips a real user disconnected by the 7-day expiry), and the seeded board renders behind the standard reconnect banner. The demo therefore exercises the real stale-but-visible path instead of a fabricated healthy one.
- Task assignment satisfies Model B: tasks assigned to each of the five users, a meaningful set **unassigned** so the claim path is demonstrable, and tasks assigned across users so the 403 path is demonstrable. A reviewer clicking the seeded board must see all three behaviours without editing data by hand.

**Proof:**
```bash
uv run python manage.py seed_demo && uv run python manage.py seed_demo && \
  uv run python manage.py shell -c "
from accounts.models import AppUser
from tasks.models import Task
from core.models import Source
print(AppUser.objects.count(), Task.objects.count(), Task.objects.filter(assignee__isnull=True).count(), Source.objects.filter(status='active').count())"
```

**Expected:** the second run prints `Seed complete (idempotent)`, then `5 28 5 0`. The final `0` is deliberate: zero active sources, because none of them have a token.

---

### Step 24 — Remaining pages and error states

**What:** Dashboard, calendar view, inbox, thread detail, admin dashboard, audit view, 403/404/500 pages, and the stale-data labels.

**Files:** `templates/**`, `tasks/views.py`, `calendar_app/views.py`, `mail/views.py`, `audit/views.py`

Every view in this app is server-rendered HTML. No SPA, no client-side state store, no build step for the frontend.

**Proof:**
```bash
uv run pytest tests/test_permissions.py -k "smoke" -q
```

**Expected:** a line containing `1 passed` — the app boots, `/healthz` is 200, and one board page renders with a seeded user.

---

### Step 25 — Scheduler wiring and the index that serves it

**What:** Wire the two sync commands to an external scheduler, and prove the scheduler's query uses the index it was built for.

**Files:** `Procfile` or `deploy/scheduler.yml`, `calendar_app/management/commands/sync_calendar.py`, `mail/management/commands/sync_gmail.py`

A hung sync must never block a page render, which is why sync is a management command and not a request path.

**Proof:**
```bash
uv run python manage.py shell -c "
from django.db import connection
c = connection.cursor()
c.execute(\"EXPLAIN SELECT id FROM core_source WHERE status='active' AND next_attempt_at < now()\")
print([r[0] for r in c.fetchall() if 'Index Scan' in r[0] or 'Seq Scan' in r[0]])" | tee /tmp/ops_plan_explain.txt
uv run grep -q 'Index Scan' /tmp/ops_plan_explain.txt && echo "INDEX_USED"
```

**Expected:** a plan row containing `Index Scan`, then `INDEX_USED`. On a small development database Postgres will correctly prefer a sequential scan, so the assertion is that the index **exists and is usable** — re-run this against a populated staging database before trusting the production plan.

---

### Step 26 — Full run, full suite, and gate readiness

**What:** One end-to-end local run against seeded data, the entire suite, the two CI greps, and a written gate-readiness report.

**Files:** `README.md` (setup and the run steps), `docs/go-live-gates.md` (G1–G5 copied from spec §4.6 with an explicit owner and a checkbox each)

**Proof:**
```bash
uv run pytest -q && \
  uv run grep -rn "^from cryptography\|^import cryptography" --include=*.py . | grep -v "accounts/crypto.py" && echo "LEAK" || echo "CRYPTO_CHOKEPOINT_OK"
uv run grep -rn "\.objects\." --include=*.py ./templates 2>/dev/null && echo "LEAK" || echo "TEMPLATES_CLEAN"
uv run grep -rn "objects\." --include=views.py . | grep -v services.py && echo "LEAK" || echo "VIEWS_HOLD_NO_QUERIES"
```

**Expected:** all tests passing, then `CRYPTO_CHOKEPOINT_OK`, `TEMPLATES_CLEAN`, `VIEWS_HOLD_NO_QUERIES`, and a `README.md` whose setup section a stranger can follow without asking a question.

`docs/go-live-gates.md` must exist and name: **G1** flip consent screen to Internal (CEO, blocked on a Workspace domain), **G2** flip signup to invite-only before any real user (engineering runs, CEO verifies), **G3** add real team members as test users, **G4** confirm `OPSHUB_TOKEN_KEY` is in the platform secret manager, **G5** run the full suite against Internal settings. G2 is the one most likely to be skipped, because nothing about it is visible until an unknown address is sitting in the audit log.

---

## Spec-to-step coverage

| Spec section | Steps |
| --- | --- |
| §1 Scope, Testing mode as target | 1, 13 |
| §2 Architecture, file structure | 1, 25 |
| §3 Schema, all six tables, all indexes | 5, 6, 7, 15, 19, 20 |
| §4.1 Scopes (`calendar.readonly`) | 8, 9 |
| §4.2 Flow, invite-or-claim | 8, 9, 10 |
| §4.3 Token storage | 4, 11 |
| §4.4 Refresh | 12 |
| §4.5 Refresh death, banner | 12, 13, 24 |
| §4.6 Testing mode lifecycle, gates | 13, 26 |
| §5 Availability algorithm | 17, 18 |
| §6 Gmail normalize, quick reply | 20, 21, 22 |
| §7 Permissions, Model B | 7, 14, 15, 16 |
| §8 Seed data | 23 |
| §9 Error handling | 12, 13, 21, 24 |
| §10 Testing strategy | 3, 4, 8–13, 15, 17–22, 24 |
| §11 Risks, rollback, gates | 13, 25, 26 |

---

## Deliberately not in this plan

- **No SPA, no client-side state.** Server-rendered HTML. There is nothing to hydrate.
- **No per-user business hours.** One 09:00–17:00 Mon–Fri constant evaluated in the requester's timezone. A nullable column plus one filter later, not a redesign.
- **No calendar write.** Not approved for phase 1, and adding it costs a full re-consent round-trip for every user.
- **No email search.** Filtering is status/priority/assignee/due-date.
- **No E2E browser tests.** There is no SPA, so the server-side tests cover the behaviour that matters.
- **No background job framework.** Scheduler-driven management commands with `next_attempt_at` backoff in Postgres. Adopt one when a sync exceeds the command timeout, not before.
- **No multi-tenancy.** One workspace, no `workspace_id`. A second team needing isolation is a System Architect decision.

Each of these passes the same test: a small, local change against an existing seam. A deferral that requires a redesign is not a deferral, it is an omission.
