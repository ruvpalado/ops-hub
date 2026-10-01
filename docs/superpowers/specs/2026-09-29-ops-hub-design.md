# Ops Hub — Design Spec

- **Date:** 2026-09-29 (amended same day)
- **Status:** Amended with six CEO decisions. No code written. No repo initialized.
- **Amendments applied:** Testing mode is the design target (§4.4, §4.6); §7 row 8 replaced with Model B, plus new row 8a; open signup retained but gated at go-live (G2); `calendar` trimmed to `calendar.readonly` (§4.1); admin mail read unchanged; `gmail.readonly` unchanged.
- **Schema amended by this revision:** `app_user.google_sub` became nullable with a partial unique index (needed for invite-only at G2); `app_user.timezone` added; `app_user.refresh_token_obtained_at` added — the last is required by the Testing-mode-as-target decision, since Google never reports refresh-token expiry.
- **Project root (as chosen):** `/home/rvpal/apps/ops-hub`
- **Spec location:** `docs/superpowers/specs/2026-09-29-ops-hub-design.md` relative to the project root above.

Project root rationale: `/home/rvpal/apps/` already holds the machine's application checkouts (`apps/G0DM0D3`). Ops Hub is a new application, not a subdirectory of an existing one, so it gets its own root there. The pre-existing empty `/home/rvpal/docs/superpowers/specs/` was **not** used: it sits outside any project and would leave the spec unattached to the code it describes.

---

## 1. Problem, Solution, Scope

### Problem

A 5–50 person operations team runs on three disconnected surfaces: a shared task list, a Google Calendar, and a Gmail inbox. The operational questions that actually cost time each week are:

1. What is the state of the shared task list, and who owns what?
2. When is everyone actually free, given that meetings overlap?
3. What arrived in my inbox, and what still needs a reply?

Answering (2) requires reading five people's calendars. Answering (3) requires living in Gmail full-time. The team has no shared surface where these three answer each other.

### Solution

A single web application that authenticates each user with Google, then presents three server-rendered views over one Postgres database: a shared task board, a calendar conflict/availability view, and a normalized inbox with quick reply. One login, one navigation, one audit trail.

Google is the sole identity provider and the sole data source for mail and calendar. Ops Hub holds a materialized, read-optimized copy plus its own local state (task board, local unread flags, audit log).

### In scope (phase 1)

| Area | Phase-1 commitment |
| --- | --- |
| Auth | Google OAuth only. Role on user record: `admin` or `member`. No passwords. |
| Tasks | Shared board. title, description, assignee, priority, due date, status, created_by. Read-shared to the whole team; edit restricted to the assignee (or an admin, or a member claiming an unassigned task) — see §7 row 8. Admin-only delete. Every read and write permission-checked. |
| Calendar | Pull Google Calendar events, merge busy blocks, detect meeting overlaps, suggest next free slot for a requested duration. |
| Inbox | Gmail threads normalized into one feed. Unread state, priority flag, quick reply sent from the signed-in user's own account. |
| Infrastructure | One web process, one Postgres. Server-rendered pages. No SPA shell. |
| Consent screen | **Testing mode, and it is the design target.** No Workspace org exists, so the 7-day refresh-token expiry is a permanent weekly operating state — handled explicitly in code and tests, not as an edge case (§4.4, §4.6). |
| Seed data | Demo team of 5 fake users plus realistic tasks, events, message threads. |
| Audit | Append-only log of who did what, admin-visible. |

### Explicitly out of phase 1

AI summarization. Knowledge base. Chat or voice interface. Slack / Trello / Teams integration. Healthcare / ITIS systems. Customizable dashboards. Learning or coaching features. Cross-device native apps.

**Go-live only, not phase 1:** invite-only signup (gate G2), and flipping the consent screen to Internal (gate G1, blocked on a Workspace domain the CEO owns). Both are configuration over code that exists from phase 1.

---

## 2. Architecture

### System

| Component | Responsibility |
| --- | --- |
| **Web process** (Django + Gunicorn, N workers) | Serves server-rendered HTML, handles form POSTs, owns all business logic, runs background sync as management commands triggered by an external scheduler. Stateless between requests — no local disk state, no in-process caches that matter. |
| **Postgres** | Single system of record. Holds users, tokens (encrypted), tasks, materialized calendar events, sync cursors, messages, audit log. |
| **Google APIs** | Outbound dependency only. Ops Hub is an OAuth client, never an identity provider. |
| **Scheduler** (cron or platform scheduler) | Kicks `python manage.py sync_calendar` and `sync_gmail` on an interval. Separate from the web process so a hung sync never blocks a page render. |

### Data flow

**Sign-in:** browser → `/auth/google` → Google consent → callback with code → web process exchanges code for tokens → tokens encrypted → `app_user` upserted → session cookie issued.

**Calendar sync:** scheduler → for each `source` with `status='active'` past `next_attempt_at` → incremental `events.list` with the stored `syncToken` → upsert into `calendar_event` by `(owner_id, google_event_id)` → advance `source.sync_cursor` → set `next_attempt_at`. `freebusy.query` is **not** called: `events.list` under `calendar.readonly` already returns `transparency`, which is where `is_busy` comes from, and one call per sync is cheaper than two.

**Gmail sync:** scheduler → for each `source` with `status='active'` past `next_attempt_at` → `users.getProfile` for `historyId` → `history.list` since the stored cursor → fetch changed message metadata + full body for changed IDs → upsert into `message` by `(owner_id, google_message_id)` → advance cursor.

Sources in `needs_reauth`, `error`, or `disconnected` are **skipped**, not retried. A user disconnected by the 7-day Testing-mode expiry therefore costs zero API calls until they reconnect.

**Page render:** browser → Django view → session → role → repository function (permission filter applied here, in one place) → template → HTML.

**Quick reply:** browser POST → validate message belongs to actor → build MIME with `In-Reply-To`/`References` → `users.messages.send` with `threadId` → on success, store the sent copy as an outbound `message` row → audit entry.

### Framework choice

**Django 5 with the built-in ORM, template engine, and admin site, served by Gunicorn.**

Against the alternative (FastAPI + SQLAlchemy + Jinja + hand-rolled admin): Django ships the pieces this app cannot skip — sessions and CSRF protection, migrations, the ORM's query construction that makes a single permission-filter choke point practical, and an admin site that gives a 5-person team a usable `/admin/` for row-level fixes on day one without building anything. The alternative is a thinner, more flexible stack that would require reimplementing all five of those as a prerequisite, and this project's hard requirements (permission checks on every read, migrations on a schema that will change) are exactly the areas where the framework's defaults are worth more than its opinions.

### File structure

```
ops-hub/
  pyproject.toml            # deps + tooling, single lockfile
  Dockerfile                # one web image; also runs migrate + collectstatic
  .env.example              # names only, never values
  manage.py
  opshub/
    settings.py             # env-driven, no hardcoded secrets
    urls.py
  accounts/                 # OAuth, session, identity
    crypto.py               # encrypt/decrypt, key loading. The ONLY importer of `cryptography`.
    google_client.py        # code exchange, refresh, expiry sweep, revocation handling
    views.py                # /auth/google, /auth/google/callback, /reconnect
    models.py               # AppUser
  tasks/                    # task board
    models.py
    views.py
    services.py
  calendar_app/             # calendar sync + conflict detection
    models.py
    google_sync.py
    availability.py         # pure functions: merge, overlaps, next_free_slot
    views.py
  mail/                     # gmail sync + inbox
    models.py
    google_sync.py
    normalize.py            # thread/message mapping
    views.py
  audit/                    # audit log writer + admin-only view
    models.py
    writer.py               # audit.record(...) — the only call site
  core/                     # permissions, pagination, error pages, logging redaction
    permissions.py          # the single choke point for role checks
    pagination.py
    redaction.py            # log filter that drops token values
  templates/
  management/commands/
    seed_demo.py
    sync_calendar.py
    sync_gmail.py
  tests/
    test_availability.py    # algorithm — highest value
    test_permissions.py     # one assertion per row of §7
    test_token_lifecycle.py # refresh, invalid_grant, redaction
    test_normalize.py       # thread mapping, reply idempotency
```

Top-level folders follow Django's app-per-bounded-context convention: `accounts`, `tasks`, `calendar_app`, `mail`, `audit` are each a domain with its own model and routes; `core` holds the cross-cutting concerns that would otherwise get duplicated into every app. `tests` is flat because the suite is small and every test file maps to one §10 area.

---

## 3. Database Schema

Six tables. Types are Postgres-native. All tables use `created_at timestamptz not null default now()` unless noted; all get a `BIGSERIAL` primary key.

### 3.1 `app_user`

Named `app_user`, not `user`, because `user` is a reserved word in Postgres and every query against it would need quoting. This is the only table name that deviates from the approved list.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | `bigserial` | no | PK |
| `google_sub` | `text` | yes | Immutable Google subject ID. The real identity key. **Nullable only for a pre-created, not-yet-signed-in row** — see §4.2 step 5 and the note below. |
| `email` | `text` | no | From the verified `email` claim only. For a pre-created row, the address an admin typed. Lowercased and trimmed on write. |
| `display_name` | `text` | no | |
| `avatar_url` | `text` | yes | |
| `timezone` | `text` | no | IANA zone, e.g. `America/New_York`. Default `UTC`. Written from the Google Calendar settings API at sync time. The single source for all business-hours arithmetic (§5.1). |
| `role` | `text` | no | Check constraint: `role in ('admin','member')`. Default `member`. |
| `access_token_enc` | `bytea` | yes | Encrypted. Null when disconnected. |
| `refresh_token_enc` | `bytea` | yes | Encrypted. Null when disconnected. |
| `token_expires_at` | `timestamptz` | yes | Access-token expiry; drives refresh. |
| `refresh_token_obtained_at` | `timestamptz` | yes | When the current refresh token was issued. **The only way to reason about Testing-mode expiry** — Google never reports a refresh token's expiry, so this column is the substitute. See §4.4. |
| `granted_scopes` | `text[]` | no | Default `{}`. What Google actually granted vs what we requested. |
| `disconnected_at` | `timestamptz` | yes | Set on revocation or Testing-mode expiry. Non-null means "needs re-auth". |
| `last_login_at` | `timestamptz` | yes | |

Indexes:
- `PRIMARY KEY (id)`
- `UNIQUE (google_sub) where google_sub is not null` — the callback's only lookup path. A duplicate would split one person's identity in two. Partial because pre-created invite rows legitimately have no `sub` yet, and they must not collide with each other on NULL under Postgres' default NULL-distinct semantics. Same partial-unique pattern as `calendar_event` in §3.3.
- `UNIQUE (email)` — the second identity guard, and the claim key for a pre-created row: first sign-in with `google_sub IS NULL` matches on lowercased email and claims the row. Also stops one human arriving with two different `sub` values from getting two accounts.
- `INDEX (role)` — admin audit view and admin user-management list filter by role. Low cardinality, but the table is small (≤50 rows) so the planner will ignore it; kept for predicate clarity, not performance.

Deliberately **not** indexed: `disconnected_at`, `token_expires_at`, `refresh_token_obtained_at`. All three are read as full-table scans over ≤50 rows — by the sync scheduler and by the Testing-mode expiry sweep (§4.4).

### 3.2 `task`

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | `bigserial` | no | PK |
| `title` | `text` | no | Check: `length(btrim(title)) between 1 and 200`. |
| `description` | `text` | yes | Check: `length(description) <= 20000`. |
| `assignee_id` | `bigint` | yes | FK → `app_user(id)` `ON DELETE SET NULL`. |
| `created_by_id` | `bigint` | no | FK → `app_user(id)` `ON DELETE RESTRICT`. |
| `priority` | `text` | no | Check: `in ('low','normal','high','urgent')`. Default `normal`. |
| `status` | `text` | no | Check: `in ('todo','in_progress','done')`. Default `todo`. |
| `due_date` | `timestamptz` | yes | |
| `completed_at` | `timestamptz` | yes | Set when status → `done`. |
| `updated_at` | `timestamptz` | no | Default `now()`. |

Indexes:
- `INDEX (status, due_date)` — the board's default query is "open tasks ordered by due date". A composite leading on `status` serves both the filter and the sort from one index; a separate `due_date` index would not help the board.
- `INDEX (assignee_id) where status <> 'done'` — the "my tasks" view, and, since §7 row 8 makes assignee the edit boundary, also the set of tasks this user is *allowed to act on*. Partial, because completed tasks assigned to a user are never queried in bulk; keeping them out shrinks the index to the working set.
- `INDEX (created_by_id)` — audit-adjacent queries ("what did X create"). Composite `(created_by_id, created_at)` would be equivalent here; the plain form is enough at this table size.

The authorization check in §7 row 8 is an `UPDATE ... WHERE id = %s AND <permission predicate>`, which resolves through the primary key. It needs no index of its own.

### 3.3 `calendar_event`

Materialized copy of a Google event, per owner.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | `bigserial` | no | PK |
| `owner_id` | `bigint` | no | FK → `app_user(id)` `ON DELETE CASCADE`. |
| `google_event_id` | `text` | no | Google's event ID. Null for seeded demo events — see below. |
| `calendar_id` | `text` | yes | Which of the owner's calendars. |
| `title` | `text` | no | Default `'(no title)'`. |
| `description` | `text` | yes | |
| `location` | `text` | yes | |
| `start_at` | `timestamptz` | no | |
| `end_at` | `timestamptz` | no | Check: `end_at > start_at`. |
| `is_all_day` | `boolean` | no | Default false. |
| `is_busy` | `boolean` | no | From Google's `transparency`/`freeBusy` response. Default true. Free/transparent events are excluded from all busy merging. |
| `event_status` | `text` | no | Check: `in ('confirmed','tentative','cancelled')`. Default `'confirmed'`. |
| `source` | `text` | no | Check: `in ('google','seed')`. Distinguishes live from demo rows. |
| `last_synced_at` | `timestamptz` | yes | |

Indexes:
- `INDEX (owner_id, start_at)` — every conflict query filters by owner then scans a time range. Composite leading on `owner_id` matches that predicate exactly; this is the highest-traffic index in the schema.
- `INDEX (start_at)` — cross-team overlap detection and the admin availability view scan by time without an owner filter.
- `UNIQUE (owner_id, google_event_id) where google_event_id is not null` — makes sync idempotent. Without it, a retried sync after a partial failure duplicates every event in the window. Seeded rows are excluded by the `IS NOT NULL` predicate, which is why the constraint is partial.
- `INDEX (owner_id, is_busy, start_at)` — availability queries always add `is_busy = true` to an owner+range predicate. This index covers that three-part filter; the two indexes above do not.

### 3.4 `source`

One row per connected integration per user. Holds sync bookkeeping and health.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | `bigserial` | no | PK |
| `user_id` | `bigint` | no | FK → `app_user(id)` `ON DELETE CASCADE`. |
| `kind` | `text` | no | Check: `in ('calendar','gmail')`. |
| `external_id` | `text` | no | Gmail: the Gmail address. Calendar: the calendar ID being synced. The `historyId`/`syncToken` cursor lives in `sync_cursor`, never here. |
| `sync_cursor` | `text` | yes | Google `syncToken` (calendar) or `historyId` (gmail). Null forces a full resync. |
| `status` | `text` | no | Check: `in ('active','needs_reauth','error','disconnected')`. Default `'active'`. |
| `last_error_code` | `text` | yes | Coarse code, never a raw API body. Check constraint over a fixed set: `invalid_grant`, `no_token`, `insufficient_scope`, `sync_token_expired`, `rate_limited`, `upstream_5xx`, `network`. Enumerated rather than free text so the admin dashboard can distinguish "Testing-mode expiry" from "user revoked access" without parsing prose, and so a `disconnected` source always says why. |
| `last_error_at` | `timestamptz` | yes | |
| `last_success_at` | `timestamptz` | yes | |
| `next_attempt_at` | `timestamptz` | yes | Scheduler's backoff gate. |

Indexes:
- `UNIQUE (user_id, kind)` — one calendar source, one gmail source per user. Enforces the one-token-one-revocation-story invariant structurally rather than in code.
- `INDEX (status, next_attempt_at)` — the scheduler's only query: "all sources where `status` is retryable and `next_attempt_at` has passed". Composite leading on `status` matches the filter; `next_attempt_at` makes the time gate sortable.

### 3.5 `message`

One row per Gmail message. Threads are represented by `thread_id`, not by a separate table — see §6.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | `bigserial` | no | PK |
| `source_id` | `bigint` | no | FK → `source(id)` `ON DELETE CASCADE`. |
| `owner_id` | `bigint` | no | FK → `app_user(id)` `ON DELETE CASCADE`. The mailbox holder. |
| `google_message_id` | `text` | no | Google's message ID. |
| `thread_id` | `text` | no | Google's thread ID. |
| `message_id_header` | `text` | yes | RFC 5322 `Message-ID`. Needed to build `In-Reply-To`. |
| `in_reply_to` | `text` | yes | |
| `subject` | `text` | no | |
| `snippet` | `text` | no | Google's snippet. |
| `body_text` | `text` | yes | Plain text part. |
| `body_html` | `text` | yes | Stored from the API. **Not rendered in phase 1** — see §6.1 and Risk 10. |
| `from_email` | `text` | no | |
| `from_name` | `text` | yes | |
| `to_emails` | `text[]` | no | Default `{}`. |
| `cc_emails` | `text[]` | no | Default `{}`. |
| `direction` | `text` | no | Check: `in ('inbound','outbound')`. Outbound rows are replies we sent. |
| `is_read` | `boolean` | no | Default false. |
| `is_priority` | `boolean` | no | Default false. Local flag, not a Gmail label. |
| `received_at` | `timestamptz` | no | |
| `has_attachment` | `boolean` | no | Default false. |
| `idempotency_key` | `text` | no | Unique per outbound send. Guards duplicate sends on retry. |

Indexes:
- `UNIQUE (owner_id, google_message_id)` — sync idempotency. Same reason as `calendar_event`: a retried sync must not duplicate a thread.
- `UNIQUE (idempotency_key)` — makes "did this reply already go out?" a single-index existence check before any resend.
- `INDEX (owner_id, is_read, received_at desc)` — the inbox list's exact query: my messages, unread first, newest first. Composite covers filter and sort.
- `INDEX (owner_id, thread_id)` — expanding a thread into its messages. Without this, opening a thread is a sequential scan of the user's whole mailbox.

### 3.6 `audit_log`

Append-only. No update path, no delete path in application code. Enforced by a Postgres rule, not by convention — see below.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `id` | `bigserial` | no | PK |
| `actor_id` | `bigint` | yes | FK → `app_user(id)` `ON DELETE SET NULL`. Null = system action (sync, scheduled job). |
| `action` | `text` | no | Dotted verb, e.g. `task.create`, `task.delete`, `mail.send`, `auth.reconnect`. |
| `resource_type` | `text` | no | `task`, `message`, `app_user`, `source`. |
| `resource_id` | `text` | yes | Stringified, because `app_user` and `task` ids live in different tables. |
| `metadata` | `jsonb` | no | Default `{}`. Never contains token material. |
| `created_at` | `timestamptz` | no | Default `now()`. |

Indexes:
- `INDEX (created_at desc)` — the admin audit view's default ordering. Without it every page load sorts the whole table.
- `INDEX (actor_id, created_at desc)` — "what did this person do", ordered by time. One composite instead of an `actor_id` index plus a sort.
- `INDEX (resource_type, resource_id)` — per-resource history (e.g. every action on task 41).
- Append-only enforcement: `CREATE RULE audit_log_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;` and the same for `DELETE`. A migration that tries to drop these rules requires written CEO approval, because it is the only thing standing between a bug and a silently empty audit trail.

Pagination: every list view is keyset-paginated with a hard cap. No view returns an unbounded result set; the cap is a constant in `core/pagination.py` (default 50).

---

## 4. Google OAuth — Highest-Risk Area

### 4.1 Client and consent screen

- **One** Google Cloud project, **one** OAuth client (Web application type), **one** consent screen.
- Requested scopes, exactly three:
  - `https://www.googleapis.com/auth/gmail.readonly`
  - `https://www.googleapis.com/auth/gmail.send`
  - `https://www.googleapis.com/auth/calendar.readonly`
- No `drive`, no `gmail.modify`, no `gmail.compose`, no `calendar` (the read/write variant), no `openid` beyond the implicit `email`/`profile` from `.../auth/userinfo.email` + `.../auth/userinfo.profile`.

**Scope decision, stated explicitly because it determines the data model (Risk 2):** `gmail.readonly`, not `gmail.modify`. Ops Hub never writes labels and never modifies Gmail's own unread state. Unread is tracked locally in `message.is_read`. The consequence the CEO should know about: reading a message in Ops Hub does **not** mark it read in Gmail, and reading it in Gmail does **not** mark it read in Ops Hub until the next sync re-reads the `UNREAD` label. This is accepted for phase 1 and is the reason `is_read` lives in our schema rather than being derived on every read.

**Calendar is `calendar.readonly`.** Requested read-only and used read-only. Phase 1 pulls events and computes availability; it never creates, moves, or deletes an event.

**Adding calendar write later requires a re-consent round-trip for every user.** Google will not widen a granted scope silently: the consent screen must be re-published, and each user must pass through the consent flow again to add `.../auth/calendar`. Under Testing mode that is a second 7-day expiry cycle on top of the first. Any future feature that writes to Calendar — booking a suggested slot is the obvious candidate — must budget for a re-consent campaign, not assume the existing grant covers it. That feature is **not approved for phase 1**; this is a cost note for whoever proposes it, not a reservation.

**Consent screen audience: Testing, and it stays Testing until a go-live step is taken.** No Google Workspace organization exists today, so Internal consent is not available. Internal requires a Workspace domain with the OAuth client's authorized domain verified, which engineering cannot provision. See §4.6 — Testing mode is the design target, not a temporary state to be engineered around.

### 4.2 Authorization-code flow

1. User hits `/auth/google`. Server generates `state` (32 random bytes, CSPRNG, base64url) and `code_verifier` (64 random bytes), stores both in the session with a 10-minute created-at, redirects to Google's consent endpoint with `access_type=offline`, `prompt=consent`, `include_granted_scopes=false`, and the three scopes.
2. Google redirects to `/auth/google/callback` with `code` + `state`. Server compares `state` to the session value with a constant-time compare, then deletes both values. Mismatch → 400, no code exchange attempted.
3. Server POSTs to `https://oauth2.googleapis.com/token` with the code, `client_id`, `client_secret`, `redirect_uri`, `code_verifier`, and `grant_type=authorization_code`.
4. Response `access_token`, `refresh_token`, `expires_in`, `scope`.
5. Server calls `userinfo` with the access token. Identity comes from the `sub` claim. `email` is read only from the verified claim; an unverified email is rejected and logged as `auth.unverified_email`.
6. Upsert on `google_sub`. Existing row: update name/avatar, **preserve `role`** — a Google profile change must never silently elevate or demote anyone. New row: create as `member`.
7. **Claim a pre-created row, if one matches.** Before creating, look for an `app_user` with `google_sub IS NULL` whose lowercased `email` equals the claimed address. If found, write `google_sub` onto that row and keep its existing `role`. This is how an admin-granted `admin` role survives to the user's first sign-in. A pre-created row that is never claimed simply never logs in; it holds no tokens and no data.
8. Store tokens encrypted. Set `refresh_token_obtained_at = now()` — only when a **new** refresh token actually arrives (§4.4). Upsert `source` rows for `calendar` and `gmail` as `active`.
9. Audit `auth.login` with the user id and IP. **No token material in the audit row.**

**Open signup is the phase-1 posture, and it is a go-live item.** There is **no email-domain allowlist and no invite requirement**: any Google account that completes the flow becomes a `member`. This is deliberate for phase 1, because the demo team runs on seeded fake users (§8) and no real Google account signs in during most of development — an allowlist would be an untested code path guarding nothing. The code path that rejects unknown emails is still built and tested in phase 1 (it is the same step-7 lookup with a `where` clause on the setting), so gate G2 is a flag flip rather than a change under time pressure.

It must not survive contact with real users. **Before the first real user is onboarded, signup flips to invite-only**, and that flip is a go-live gate (§4.6). Invite-only is not new code: it is the step-7 claim path plus a rejection branch that redirects an unmatched `sub` to a 403 page naming the address that tried to sign in, so an admin can see who was turned away and pre-create their row. The gate is running it, not writing it.

### 4.3 Token storage

- Encryption: AES-256-GCM. Random 96-bit nonce per encryption, stored as a versioned envelope: `bytea = [version:1][nonce:12][ciphertext][gtag:16]`. Version byte is byte 0, so a future key rotation is additive and old rows stay readable.
- Key: 32 bytes, base64, from env var `OPSHUB_TOKEN_KEY`. Never in the database, never in code, never in a log line, never in a committed file. `.env.example` lists the name and a generation command, not a value.
- `accounts/crypto.py` holds the only encrypt/decrypt functions. No other module imports `cryptography` directly — enforced by a CI grep, because "plaintext tokens exist in exactly one place" is a claim that is only true if something prevents a second place from appearing.
- The `redaction.py` log filter drops any field whose key matches `token`, `access_token`, `refresh_token`, `client_secret`, `authorization`, or `code`. It is installed on the root logger in `settings.py`, and `tests/test_token_lifecycle.py` asserts a synthetic token never appears in captured log output. Defense in depth: even if the filter misses, the next layer is that plaintext tokens exist only inside the two functions above.
- **Access tokens are held in memory for the duration of a request only.** They are not written anywhere outside the encrypted columns.

### 4.4 Refresh handling

- `token_expires_at` is stored on the user row. Checked before every Google API call.
- If `token_expires_at - now() < 60s`, refresh first.
- Refresh is **lazy and per-request**, not a background timer. A token expiring mid-request is recovered by one refresh attempt inside the request that needed it.
- On successful refresh: replace `access_token_enc`, update `token_expires_at`, append the new scope set to `granted_scopes` (Google may return fewer scopes than requested; we record what we actually got).
- On refresh failure:
  - `invalid_grant` → the refresh token is dead. Set `source.status = 'needs_reauth'` for both kinds, `source.last_error_code = 'invalid_grant'`, set `app_user.disconnected_at = now()`, **do not delete the encrypted token**, audit `auth.refresh_revoked`. The user sees a banner on their next page load: "Your Google connection expired. Reconnect to resume sync." Their existing board, tasks, and cached mail stay visible and readable — disconnecting an integration degrades freshness, it does not gate access to the app.
  - `5xx` or network error → transient. Retry once with jittered backoff (2s, then 8s), then surface as `source.status='error'` with `source.last_error_code='upstream_5xx'` and `next_attempt_at` set 15 minutes out. Do **not** disconnect on a transient error.
- **A missing refresh token on the callback response is not an error.** Google omits `refresh_token` when the user has already granted access and `prompt` is not `consent`. In that case the existing encrypted refresh token is kept, and `refresh_token_obtained_at` is left unchanged. Losing it would sign the user out for no reason.

#### The 7-day refresh-token clock

Google never reports a refresh token's expiry date. Under Testing mode it silently dies 7 days after issuance, and the only signal that arrives is an `invalid_grant` on some later refresh attempt. That is a reactive-only design: the user discovers the expiry by hitting a failed sync, not by being told a week in advance.

`refresh_token_obtained_at` makes the window knowable. Because Testing mode is the design target (§4.6), this is not an edge case to be discovered in production — it is a scheduled, weekly, certain event for every user forever. Two mechanisms:

- **Proactive banner.** A scheduled sweep reads `refresh_token_obtained_at` and, at day 5 of 7, flags the user. The banner reads "Your Google connection expires in 2 days — reconnect now to avoid a sync interruption." It links to `/auth/google`. Doing the reconnect early, while the old token still works, means the user never sees sync break.
- **Reactive recovery.** If the sweep missed it and a refresh returns `invalid_grant`, the §4.5 path runs: `needs_reauth`, banner, data preserved, explicit reconnect prompt. The message names the likely cause rather than saying "invalid_grant".

The sweep reads ≤50 rows and is a single unindexed scan (§3.1). It is one query in the existing scheduler tick, not new infrastructure.

**Why this matters more than it looks:** a user who does not reconnect after the 7-day expiry does not get degraded sync, they get a feed that silently stops updating. "Disconnected" must be a loud, specific, unmissable state with a working fix, because the alternative is a team quietly working from stale data and assuming it is current.

### 4.5 When a refresh token dies

Three causes, one user-visible outcome. **Because Testing mode is the design target, the middle row is not an edge case — it is a weekly scheduled event for every user, indefinitely.**

| Cause | Frequency under Testing | Detect | Action |
| --- | --- | --- | --- |
| **Testing-mode 7-day expiry** | **Every 7 days, per user, forever** | `invalid_grant` on refresh, with `refresh_token_obtained_at` older than 7 days | `needs_reauth`, specific banner naming the 7-day cause, data preserved |
| User revoked access at `myaccount.google.com` | On demand | `invalid_grant` on refresh, token younger than 7 days | `needs_reauth`, banner, data preserved |
| Admin removed the user from the OAuth client / app deleted | Rare | `invalid_grant` on refresh | Same, plus the admin sees the user greyed out in `/admin/` |

The first two rows are distinguishable because `refresh_token_obtained_at` tells us which one happened, so the banner can say "this expires every 7 days while the app is in testing mode" rather than a generic error. That distinction is the entire reason the column exists.

In all cases: no data is deleted, no task is lost, no mail row is removed. `next_attempt_at` is set to `now() + 7 days` so the scheduler does not hammer Google with doomed refresh attempts. A `needs_reauth` source is skipped entirely by the scheduler until the user reconnects.

**The failure mode to design against:** a disconnected user whose feed silently stops updating. The app must never look healthy while stale. Every page load for a user with `disconnected_at IS NOT NULL` shows a persistent, non-dismissible banner with a working reconnect link, and the inbox and calendar views label their content as stale with the age of the data. "Working but quietly wrong" is worse than "visibly broken", because a team will make decisions on the stale data.

Reconnect flow: `/auth/google` again → normal code flow → tokens replaced → `refresh_token_obtained_at = now()` → `source.status` back to `active` → `next_attempt_at = now()` → a full resync runs on the next scheduler tick (cursor is preserved when still valid, so the resync is incremental). No data loss, no full re-import of mail.

### 4.6 Testing mode is the design target

**No Google Workspace organization exists.** The consent screen is therefore in **Testing** mode, and stays there until a go-live step is taken. Engineering cannot change this: Internal consent requires a Workspace domain with a verified authorized domain, and that is a company-provisioning task owned by the CEO.

This changes the design posture. Testing mode is not a temporary condition to be engineered around until someone flips a switch — **it is the environment the app is designed and verified in.** The 7-day refresh-token expiry is a permanent, weekly, certain event for every user, and the system is built to handle it as a normal operating state rather than an exception path.

| Behavior | Consent screen = **Testing** (design target) | Consent screen = **Internal** (future go-live step) |
| --- | --- | --- |
| Refresh token lifetime | **Expires 7 days after the user grants access.** Not sliding, not extended by use. Weekly, forever, for every user. | Does not expire. Revoked only by explicit user action or admin removal. |
| User cap | **100 test users total.** Sign-in fails for anyone not on the test-user list, added by email in the Google Cloud console. | No cap. |
| Unverified-app interstitial | Yes. Users see "Google hasn't verified this app" and must click Advanced → "Go to Ops Hub (unsafe)" on first consent. | No. |
| Warning emails | Google emails the project owner about the unverified app and the expiry. | None. |
| Engineering implication | **Every user must re-consent weekly.** The reconnect path is exercised constantly, so it will be the best-tested code in the project. §4.4's day-5 proactive banner exists specifically for this. | Reconnect becomes rare. The path still exists and is still tested — it is just no longer load-bearing. |
| Test coverage required | Mandatory: 7-day expiry, revoked token, transient 5xx. §10.3. | Same tests still run. Nothing is deleted when flipping. |

**Consequences accepted for phase 1:**

1. Every user reconnects weekly. The reconnect UX is a first-class feature, not an error page — it gets its own route, its own messaging, and its own tests.
2. The 7-day expiry is handled **explicitly in code and in tests** (§4.4, §10.3), not treated as an edge case discovered in production.
3. The 100-user test-user cap bounds the team. At 5–50 people this is not binding; it is a fact to remember at 100, not a constraint now.
4. Anyone signing in must be added as a test user first. During most of phase 1 nobody real signs in at all — the demo team is seeded (§8).

**Flipping Testing → Internal is a configuration change in Google Cloud, not a code change:**

1. CEO obtains a Google Workspace domain.
2. Add the domain to the OAuth client's Authorized Domains.
3. Verify the domain (DNS TXT record).
4. Publish the consent screen, change the audience to Internal.

Two things to know before doing it. **Existing refresh tokens issued under Testing keep their 7-day expiry** — the flip does not extend them, so expect one final re-auth wave for anyone who signed in under Testing, then none. And this is not obvious from Google's documentation, so it goes in the runbook rather than in someone's memory.

**Go-live gate list.** Each item is owned and checked before real users are onboarded:

| # | Gate | Owner | Why |
| --- | --- | --- | --- |
| G1 | Flip consent screen to Internal per the four steps above | CEO | Removes the weekly re-auth and the 100-user cap. |
| G2 | Flip signup from open to **invite-only** (§4.2 step 5) | Engineering runs, CEO verifies | Open signup on a live app means anyone with a Google account can read the task board. Must not be the state real users first see. |
| G3 | Add every real team member as a test user while still in Testing mode | CEO | Required to complete a first sign-in at all. |
| G4 | Confirm `OPSHUB_TOKEN_KEY` is in the platform secret manager, never in a repo | CEO / platform | Risk 4's control. |
| G5 | Run the full test suite against Internal-configured settings | Engineering | Same tests, new audience. Catches config drift. |

G2 is the one most likely to be skipped, because it is invisible until someone unknown is in the audit log. It stays on the list.

---

## 5. Calendar Conflict Detection

Pure functions in `calendar_app/availability.py`, no I/O, no Google calls. Inputs are already-materialized `calendar_event` rows. This is the most heavily tested code in the project.

### 5.1 Timezone rule

**All comparisons are in UTC.** Every `calendar_event.start_at`/`end_at` is stored as `timestamptz` in UTC, converted from the event's own timezone at sync time (Google returns `dateTime` with a `timeZone` field; all-day events use the calendar's timezone). One timezone for the whole algorithm removes an entire class of bugs. Business-hours arithmetic is the only place a local timezone appears, and it uses the **requester's** `app_user.timezone` (§3.1) — not the event owner's, and not a per-user configurable value.

### 5.2 Merge busy blocks

Input: a list of busy intervals for one owner over `[window_start, window_end)`, each `{start, end}` with `end > start`, possibly with `is_busy = false` and possibly `event_status = 'cancelled'`.

1. Filter: drop `is_busy = false` and `event_status = 'cancelled'`. A declined or transparent event occupies no time.
2. Clip: `start = max(start, window_start)`, `end = min(end, window_end)`. Drop intervals where `end <= start` after clipping.
3. Sort ascending by `start`, tie-break by `end` ascending.
4. Sweep: maintain `merged`. For each interval, if `merged` is empty or `interval.start > merged.last.end`, append a new block. Otherwise extend `merged.last.end = max(merged.last.end, interval.end)`.
   - **Adjacency is merged.** `end == next.start` merges into one block. Rationale: a free slot requires strictly positive duration, so a zero-length gap between two events is not a slot. Merging at `>` vs `>=` is the difference between an off-by-one-duration bug in every downstream calculation and none.
5. Return `merged`.

Output is a sorted list of disjoint intervals with `end_i <= start_{i+1}`, no overlap, no zero-length gaps.

### 5.3 Detect overlaps between meetings

For a given owner over a window, an overlap is a pair `(a, b)` with `a.id != b.id`, both passing the §5.2 filter, and:

```
a.start_at < b.end_at  AND  b.start_at < a.end_at
```

The SQL shape, which uses `idx_calendar_event_owner_start` and `idx_calendar_event_start`:

```sql
SELECT a.id AS a_id, b.id AS b_id,
       GREATEST(a.start_at, b.start_at) AS overlap_start,
       LEAST(a.end_at, b.end_at)     AS overlap_end
FROM calendar_event a
JOIN calendar_event b
  ON a.owner_id = b.owner_id
 AND a.id < b.id
 AND a.start_at < b.end_at
 AND b.start_at < a.end_at
WHERE a.owner_id = %s
  AND a.start_at < %s   -- window_end
  AND a.end_at   > %s   -- window_start
  AND a.is_busy
  AND b.is_busy
  AND a.event_status <> 'cancelled'
  AND b.event_status <> 'cancelled'
ORDER BY overlap_start;
```

Output is grouped by day for display. An overlap shorter than 5 minutes is flagged but visually de-emphasized — back-to-back definitions of 2 minutes are not an operational problem, and treating them as one makes the feature cry wolf.

**Cross-owner overlap is not reported.** "Overlaps between meetings" means one person's meetings colliding with that same person's other meetings. Two people being double-booked is a different question, answered by §5.4, not this one. Reporting it here would conflate a personal scheduling mistake with a team conflict.

### 5.4 Next free slot

Input: `owner_ids` (the people who must attend), `duration_minutes` (30/45/60/90, validated against a fixed enum — not free-text), `window_start`, `window_end` (each capped at 31 days), `timezone` (the requester's business timezone), `now`.

Output: up to 5 candidate start times, ranked by preference. The function returns a definite empty list when nothing fits — it never invents a slot.

1. **Collect busy blocks.** For each owner in `owner_ids`, fetch their busy events in the window (§5.2 filter applied). The query is bounded by `(owner_id, start_at)` and the window, so it never loads an unbounded set.
2. **Union all owners' busy blocks.** Run the §5.2 merge across the *combined* list from all owners. Two people busy at the same time produce one block, not two — the algorithm is set-union, so duplicates are absorbed for free.
3. **Generate candidate windows.** Intersect `[window_start, window_end)` with the requester's business hours in the requester's timezone:
   - Business hours: `BUSINESS_HOUR_START` (09:00) to `BUSINESS_HOUR_END` (17:00), **Monday–Friday**, defined as a module constant. This is a deliberate simplification: no per-user business-hour configuration in phase 1. Adding it later is a nullable column on `app_user` plus a filter here — one function, not a redesign.
   - Business hours are **inclusive-exclusive**: a 09:00–17:00 day yields the interval `[09:00, 17:00)`. A meeting may end exactly at 17:00.
   - Iterate day by day. Skip days whose business window lies entirely in the past (`end <= now`). Also skip Saturday and Sunday.
   - All arithmetic is done in the requester's timezone using `zoneinfo`, then converted to UTC for comparison against busy blocks. Python 3.9+ `zoneinfo` is stdlib — no `pytz` dependency.
4. **Subtract busy from each candidate window.** For each business-hours interval `W`, walk the merged busy list in order:
   - `cursor = W.start`
   - For each busy block `B` that intersects `W`: if `B.start > cursor`, then `[cursor, B.start)` is a free gap; set `cursor = max(cursor, B.end)`.
   - After the loop, `[cursor, W.end)` is a free gap.
   - Drop any gap shorter than `duration_minutes`.
   - **Edges matter.** A free gap of exactly `duration_minutes` is a valid slot. A gap one minute short is not. No rounding, no tolerance.
5. **Rank candidates.** From the ordered list of free gaps, take gaps with `gap_length >= duration_minutes` and produce candidate start times on a 15-minute grid aligned to the local clock, not to the gap's start. So a gap beginning at 09:07 yields candidates at 09:15, 09:30, …, not 09:07. Aligning to the gap start produces slots like "9:07am" that nobody wants.
   - Round **up** to the next grid point. If that round-up leaves less than `duration_minutes` of gap, drop that candidate.
   - Emit at most 5, ordered by start time ascending.
   - Within the same day, earlier is preferred. There is no cleverness about "the slot that disturbs the fewest people" in phase 1.
6. **No-slot case.** Return `[]` with a reason: `"no_window"` (window too small), `"outside_hours"` (all conflicts were with non-business time), or `"fully_booked"`. The UI shows the reason. Silent empty results are the failure mode that makes people stop trusting a scheduling tool.

Worked example, so there is no ambiguity at implementation time:

- Requester timezone `America/New_York`, duration 60 min, window Mon 2026-09-28 00:00 → Wed 2026-09-30 23:59 local.
- Mon 09:00–17:00 local. Busy blocks (UTC) covering Mon: `13:00–14:00Z` (09:00–10:00 ET), `15:00–16:30Z` (11:00–12:30 ET), `17:00–18:00Z` (13:00–14:00 ET).
- Merged: `13:00–14:00`, `15:00–16:30`, `17:00–18:00`. Note `14:00Z` and `15:00Z` do **not** merge — there is a genuine 60-minute gap at `14:00–15:00Z` (10:00–11:00 ET).
- Free gaps after subtraction: `[14:00, 15:00)` = 60 min exactly → valid. `[16:30, 17:00)` = 30 min → too short. `[18:00, 21:00)` = 180 min → valid.
- 15-minute grid aligned to local clock: `14:00Z` = 10:00 ET, already on grid. Candidates from that gap: `14:00Z` (10:00 ET). Candidates from `[18:00, 21:00)`: `18:00Z` (14:00 ET), `19:00Z` (15:00 ET), `20:00Z` (16:00 ET) — `20:00Z` + 60 min = `21:00Z`, exactly the window end, so it is emitted; the grid point `21:00Z` would produce a zero-length tail and is dropped.
- Result: `[14:00Z, 18:00Z, 19:00Z, 20:00Z]` → 4 candidates on Monday, 10:00 ET, 2:00 pm, 3:00 pm, 4:00 pm.
- The window runs to Wednesday, so the walk continues. Tuesday 2026-09-29 has no busy blocks at all, so its first gap is the whole business day and `09:00 ET` = `13:00Z` is a candidate. That is the fifth candidate, and the cap of 5 stops there. **The earlier draft of this line said "4 candidates" and stopped at Monday; that contradicted the stated cap and the day-by-day walk, since a five-candidate result was available. The rule is authoritative: emit at most 5, ordered by start time ascending.**

---

## 6. Gmail Normalization

### 6.1 Thread → message mapping

**Decision: one `message` row per Gmail message, no `thread` table.** Threads are a grouping over messages, and every field a feed needs — subject, snippet, participants, unread — can be derived from a `DISTINCT ON (thread_id)` query against an index we already have. A `thread` table would need denormalization and its own invalidation path, for a query Postgres already answers in one pass.

Feed query:

```sql
SELECT DISTINCT ON (thread_id) *
FROM message
WHERE owner_id = %s
ORDER BY thread_id, received_at DESC;
```

followed by an in-Python pass to compute per-thread unread count and the `to_emails` union across the thread's messages. Bounded by the page size.

A thread displays as one feed row, anchored on its **most recent message**:

| Feed column | Source |
| --- | --- |
| Subject | Latest message's `subject` |
| Snippet | Latest message's `snippet` |
| Participants | Union of `from_email` and `to_emails` across the thread's messages in the page |
| Last activity | Max `received_at` |
| Unread | `count(messages where is_read = false) > 0` — the whole thread is unread if any message is |
| Priority | True if **any** message in the thread is priority |

The unread-any rule is a choice, not a consequence. Gmail's own unread state is per-message, but a person reads a thread top to bottom, so a partially-read thread reads as "read" to a human. If the team prefers per-message, that is a one-line change in the aggregation.

Thread expansion on click: `WHERE owner_id = %s AND thread_id = %s ORDER BY received_at ASC`, served by `idx_message_owner_thread`. Single indexed query, no N+1.

**Not done in phase 1:** attachment content, HTML body rendering, thread archiving. `body_html` is stored but not rendered; the UI shows `body_text`, falling back to a stripped-text version of the HTML if no plain part exists.

### 6.2 Unread state, and how it stays in sync

Local truth lives in `message.is_read`. Two writers, one rule.

**Writer 1 — sync, from Gmail.** On each sync, for any message whose `UNREAD` label changed:
- Gmail now says unread, we had it read → set `is_read = false`. A Gmail client marked it unread; Ops Hub follows.
- Gmail now says read, we had it unread → set `is_read = true`. Someone read it in Gmail; Ops Hub follows.
- No change → no write. This is the common case and it keeps idle syncs cheap.

**Writer 2 — the Ops Hub UI.** Opening a thread sets `is_read = true` for every message in that thread. This does **not** call Gmail's modify endpoint — we do not hold `gmail.modify`, per §4.1.

**The conflict, stated plainly:** a user can mark a thread read in Ops Hub, then mark it unread in Gmail, and Ops Hub will not see that until the next sync, which is at most one scheduler interval away (target ≤5 minutes). And a user who marks a thread read in Ops Hub will find it unread again in Gmail. Both are accepted. The alternative — holding `gmail.modify` and writing read-state back to Gmail — couples our inbox state to their mail client and makes Ops Hub responsible for a state the user also owns elsewhere.

**Sync mechanics.** `users.getProfile` returns `historyId`. Stored in `source.sync_cursor`. Each sync calls `history.list` with `startHistoryId`; on a non-empty history, fetch metadata for the changed message IDs in batches of 100, then full bodies only for messages not already stored. Update `source.sync_cursor` to the new `historyId` only after the upserts commit.

On a fresh account with no cursor: seed with `in:anywhere newer_than:30d`, cap the initial import at the most recent 200 threads, then set the cursor. A team member with a decade of mail does not get a decade imported on first login.

**Label refresh.** `users.labels.get` for `UNREAD` gives its ID once and it is cached in-process for the sync run. Two extra API calls per sync at most, not per message.

### 6.3 Quick reply

1. User types in the reply box on a thread. The form posts to `/inbox/thread/<id>/reply`.
2. Server validates: the thread exists, and `thread.owner_id == request.user.id`. A mismatch is a 404, not a 403 — the existence of someone else's mailbox should not be confirmable by the response code.
3. Build an RFC 5322 message:
   - `To`: the thread's latest message `from_email`, unless that is the sender's own address, in which case the first non-self `to_emails` entry.
   - `Subject`: reuse the thread subject verbatim, including its `Re:` prefix. Do not re-prefix.
   - `In-Reply-To`: latest message's `message_id_header`.
   - `References`: thread's earlier `message_id_header` values, space-separated, in order.
   - `Message-ID`: generated locally with `make_msgid`.
4. **Idempotency key.** The client renders a UUID into the form as a hidden field. Before sending, the server checks `SELECT 1 FROM message WHERE idempotency_key = %s`. If a row exists with `direction='outbound'`, the send is skipped and the existing result returned. This makes double-clicks, back-button resubmits, and retry-after-timeout all safe — the one place in this app where a duplicate email is genuinely harmful.
5. Send via `users.messages.send` with `threadId` so Gmail files it into the right conversation.
6. On success: insert an outbound `message` row with the returned `google_message_id`, `direction='outbound'`, `is_read=true`, `received_at=now()`, and the `idempotency_key`. Audit `mail.send` with thread id and recipient count.
7. On failure: see §9. The draft text is preserved client-side; nothing is stored as sent.

---

## 7. Permission Model

**Rule 0 — enforcement is structural, not per-view discipline.** Every view calls `require_member(request)`, which resolves the session to an `app_user` and a role, or 302s to sign-in. Every database read and write goes through a function in `tasks/services.py`, `calendar_app/views.py`, or `mail/views.py` that takes the actor as its first argument and applies the filter in the query itself. Views contain no ORM calls of their own. A grep for `objects.` outside the services layer is a CI check that fails the build — this is the enforcement mechanism for Risk 3, and it is the single most important line in this section.

"Member" means an `app_user` row with a non-null `google_sub` and a role in the enum, on the team's single workspace. A pre-created row with `google_sub IS NULL` (§4.2 step 7) is an **invitation, not a user**: it cannot sign in, holds no tokens, and appears in no list view or audit read. This is a single-team tool, not a multi-tenant one — there is no `workspace_id`, and adding one later is a migration plus one filter per query.

**Every permission predicate belongs in the SQL, not in Python.** The pattern throughout: the actor's identity is a query parameter, never a branch taken after the row is fetched. Two reasons, and both have bitten this class of bug before — a read-then-check leaves a TOCTOU race where two concurrent requests both pass the read, and a check in a view is a check someone will forget in the next view. A denied operation updates zero rows; the caller sees that and returns 403.

| # | Resource | Action | admin | member | Enforcement |
| --- | --- | --- | --- | --- | --- |
| 1 | `app_user` | Sign in via Google | Yes | Yes | Public `/auth/google`. In the phase-1 posture an unlisted email creates a `member`; the same code path rejects it when invite-only is on (§4.2 step 5, gate G2). An admin can pre-create a row to grant `admin` or an invite seat at first sign-in. |
| 2 | `app_user` | Read own profile, own tokens | Yes | Yes | `WHERE id = actor.id`. No path ever returns another user's token columns. |
| 3 | `app_user` | Read team directory (name, email, role) | Yes | Yes | Full list, token columns excluded at the serializer. Needed for the assignee picker. |
| 4 | `app_user` | Change another user's role | Yes | **No** | `require_role(actor, 'admin')` → 403. Audit `user.role_change`. |
| 5 | `app_user` | Read audit log | Yes | **No** | `require_role(actor, 'admin')` → 403. Audit access is itself audited (`audit.read`). |
| 6 | `task` | List / read | Yes | Yes | The team board is shared. That is the product. `SELECT ... WHERE status ...`, no owner filter. |
| 7 | `task` | Create | Yes | Yes | `created_by_id = actor.id`, forced server-side; a client-supplied `created_by` is ignored, not trusted. |
| 8 | `task` | Update (title, description, assignee, priority, due, status) | Yes | **Own only** | **Model B.** The update is permitted when: the actor is the assignee; **or** the task is unassigned (`assignee_id IS NULL`) and the actor is claiming it by assigning it to themselves; **or** the actor is an admin. A member editing a task assigned to someone else gets **403**. Enforced in the `UPDATE`'s `WHERE` clause: `WHERE id = %s AND (assignee_id = %s OR assignee_id IS NULL OR %s = 'admin')`, so an unauthorized edit updates zero rows and is indistinguishable from a no-op rather than a partial write. Audit `task.update` with actor, prior assignee, and changed fields. |
| 8a | `task` | Reassign to a different user | Yes | **Own only** | Same predicate as row 8, evaluated against the task's assignee **before** the change. An assignee may hand their own task off; a member may not take an unassigned task and immediately push it to a third person without it passing through an admin — `assignee_id IS NULL` permits claiming (setting it to yourself) or leaving it unassigned, and permitting any other target requires admin. |
| 9 | `task` | Delete | Yes | **No** | `require_role(actor, 'admin')` → 403. Hard delete, `ON DELETE RESTRICT` on `created_by`. Audit `task.delete` with the full prior row in `metadata`. |
| 10 | `task` | Bulk delete | Yes | **No** | Same guard, applied per id. Never a blanket `DELETE ... WHERE id IN (...)` without the role check. |
| 11 | `calendar_event` | Read own | Yes | Yes | `WHERE owner_id = actor.id`. |
| 12 | `calendar_event` | Read another user's (title, times only) | Yes | Yes | Required for availability. Excludes `description` and `location`, which can hold private content. Serialized separately from the owner view. |
| 13 | `calendar_event` | Create / update own | Yes | Yes | `WHERE owner_id = actor.id` forced into the update's `WHERE`, not checked after. |
| 14 | `calendar_event` | Update / delete another user's | Yes | **No** | `require_role(actor,'admin')` → 403. A member cannot move a colleague's meeting. |
| 15 | `message` | List / read own inbox | Yes | Yes | `WHERE owner_id = actor.id`. |
| 16 | `message` | Read another user's mailbox | **No** | **No** | **Admin has no mail visibility.** Mail is private to its owner at every role. This is the single most surprising line in this table and it is deliberate. Admin power over mail is not required by any approved feature; the audit log already covers "role-scoped admin visibility". |
| 17 | `message` | Mark read/unread, toggle priority | Yes | Yes | `WHERE owner_id = actor.id AND id = %s` in the UPDATE's `WHERE` clause. |
| 18 | `message` | Send reply | Yes | Yes | `WHERE owner_id = actor.id AND thread_id = %s`. Sends as the actor's own account by construction — the token used is the actor's, so impersonation is structurally impossible rather than merely forbidden. |
| 19 | `source` | Read own sync status | Yes | Yes | `WHERE user_id = actor.id`. |
| 20 | `source` | Read all users' sync status | Yes | **No** | Admin-only dashboard row. No token columns, ever. |
| 21 | `source` | Force resync | Yes | Yes (own only) | `WHERE user_id = actor.id` for members; admins may target any `source.id`. |
| 22 | `source` | Disconnect an integration | Yes | Yes (own only) | Deletes encrypted tokens, sets `status='disconnected'`. Data rows survive. |
| 23 | `audit_log` | Read | Yes | **No** | `require_role(actor,'admin')`. |
| 24 | `audit_log` | Write | System only | **No** | Only `audit/writer.py` writes. No request handler constructs an `audit_log` row directly. |
| 25 | `audit_log` | Update / delete | **No one** | **No one** | Postgres rules make these no-ops (§3.6). |

**The two rows a reviewer should push back on: #8 and #16.**

- **#8 / #8a (Model B)** makes the assignee an edit boundary. A member cannot retitle, re-prioritize, reassign, or close a task assigned to a colleague. Two consequences worth stating plainly rather than discovering in use:
  - **Reading and writing differ.** Row 6 lets every member read the whole board — that is the shared surface and the product. Row 8 means they cannot edit most of what they can see. The board is deliberately a read-shared, write-scoped surface. A member looking at a colleague's task sees the edit controls absent, not disabled-and-teasing.
  - **Row 6 and row 8 together leave a coordination gap**, and it is intentional: if a task is stuck with someone who is on leave, only an admin can move it. On a 5-person team an admin is always reachable. The audit log records every edit, so nothing is lost when someone does help.
  - **The unassigned-task carve-out is the load-bearing detail.** Without it, an unassigned task belongs to nobody and therefore to no one, and unclaimable tasks would be dead rows. A member may claim an unassigned task by assigning it to themselves; they may not claim it on someone else's behalf (#8a).
  - Enforcement is in the `UPDATE`'s `WHERE`, not a read-then-check. A check-then-write leaves a race where two requests both pass the read and both write; a predicate in the `WHERE` cannot.
- **#16** denies admins read access to anyone's mail. The argument for: no approved feature needs it, mail content is the most sensitive data in the system, and admin visibility is satisfied by the audit log. The argument against: an admin troubleshooting "why didn't my reply send" has to ask the user. That is the intended cost.

**One rule that follows from Model B and is easy to get wrong:** the assignee check must compare against the task's assignee **as it was before the edit**, not after. If the predicate reads the post-update value, a member can satisfy it by first reassigning a colleague's task to themselves. The `UPDATE ... WHERE` form above takes the assignee from the row being updated, which is why it is safe and a Python-side check is not.

---

## 8. Seed Data

Purpose: make the whole app demonstrable, and make the algorithm and permission layers testable, without any real Google account. Real user data is not required in phase 1.

`manage.py seed_demo` is idempotent (TRUNCATE + INSERT), and refuses to run unless `OPSHUB_ENV=development` is set explicitly. It cannot be invoked against a production database by accident.

**Users — 5.**
`google_sub = 'seed-user-1'…'seed-user-5'`. This prefix is the safety mechanism: Google's `sub` values are numeric strings, so a seeded user can never collide with a real sign-in, and a real sign-in can never silently take over a seeded row. Roles: `ada.admin@example.test` is `admin`, the other four are `member`. Names drawn from a diverse, ordinary set.

**Seed `source` rows are `disconnected`, not `active`.** All token columns are NULL, and a `source` with no token is not connected — so the honest status is `disconnected`, with `disconnected_at` set and `last_error_code='no_token'`. The scheduler skips these sources, which is exactly the behaviour §4.5 requires of a disconnected user. The seeded board still renders in full, behind the standard reconnect banner — so the demo exercises the stale-but-visible path rather than a fabricated healthy one. A first real sign-in creates a genuinely `active` source and forces a full sync, because `sync_cursor` is NULL.

**Task assignment in seed data must satisfy Model B.** With edit scoped to the assignee (§7 row 8), a board where every task is assigned to someone is a board where most members can do nothing. The seed therefore includes: tasks assigned to each of the five users (so every member has something they can act on in a demo), a meaningful set of **unassigned** tasks (so the claim path is demonstrable), and tasks assigned across users (so the 403 path is demonstrable). A reviewer clicking around the seeded board must be able to see all three behaviours without editing data by hand.

**Tasks — 28.**
Mix: 9 `todo`, 6 `in_progress`, 13 `done`. Priorities across all four levels, skewed to `normal`. 20 assigned across all five users, 5 unassigned, 3 assigned to a user who also has `done` tasks (so the partial `assignee` index is exercised). 4 overdue, 6 due this week, rest due over the next three weeks. 6 with a description of 2+ sentences, 2 with an empty description (so the UI's null path is exercised). Content is realistic ops work: vendor contract renewal, contractor laptop ordering, quarterly access review, a flaky integration, a customer escalation. Created-by is cross-populated so the board shows work flowing between people.

**Calendar events — 46, across 14 days, for all five users.**
- 1 daily recurring standup, 15 min, 09:15 ET, all five attendees modeled as five per-user rows.
- 6 one-on-ones, 30 min.
- 12 focus blocks with `is_busy = false` — present in the calendar, correctly excluded from all busy merging. These exist to fail a naive implementation that forgets step 1 of §5.2.
- 3 deliberate double-bookings for the same user, one of them only 3 minutes of overlap, so §5.3's 5-minute threshold has something to act on.
- 1 event spanning a weekend.
- 1 all-day event (`is_all_day = true`) crossing two days.
- 2 events back-to-back with `end == next.start`, to prove adjacency merging.
- 1 event ending exactly at 17:00 ET, to prove the inclusive-exclusive business-hours rule.
- All rows `source='seed'`, `google_event_id=NULL`, which is what the partial unique index in §3.3 is shaped for.

**Messages — 22 threads, 51 messages.**
Realistic ops threads: a vendor invoice dispute (4 messages), a customer escalation with a reply chain (6), a recruiter outreach (1, inbound only), an internal ops thread (5), two broadcast announcements (1 each). Mix: 14 unread, 5 read, 3 priority-flagged, 4 with `has_attachment = true` (flag only, no content). `message_id_header` populated with syntactically valid RFC 5322 IDs in a consistent chain, so the §6.3 reply builder produces correct `In-Reply-To` and `References` headers against seeded data. Two outbound messages from seeded users, so the feed shows sent mail.

Seed data is regenerable and disposable. It is never the source of truth for anything.

---

## 9. Error Handling

### 9.1 Global

- Structured logging, `request_id` on every line, propagated from a middleware. A user-reported problem is traceable to log lines without asking the user for a timestamp.
- **Token redaction is a log filter, applied at the root logger.** `core/redaction.py` drops any field keyed `token`, `access_token`, `refresh_token`, `client_secret`, `authorization`, or `code`. A unit test asserts a synthetic token string never appears in captured output. This is Risk 4's control, and it is tested rather than assumed.
- User-facing errors are generic and carry the `request_id`. No stack traces, no upstream error bodies, no API responses rendered into HTML.
- Upstream Google error bodies are logged at `DEBUG` only and truncated to 2 KB. They can contain subject identifiers.
- Every state-changing action writes an audit row in the same transaction as its effect, so the log cannot drift from reality.

### 9.2 Google auth

| Failure | User sees | Logged | Retried |
| --- | --- | --- | --- |
| `state` mismatch / expired | 400, "Sign-in expired. Try again." | `WARN auth.state_mismatch` + request_id. No code value. | No. |
| Code exchange `invalid_grant` | "That sign-in link has expired. Try again." | `WARN auth.code_rejected` | No. |
| Code exchange 5xx | "Google sign-in is temporarily unavailable." | `ERROR auth.token_endpoint_5xx` | Once, 2s, jittered. |
| Userinfo 401 | Treated as `invalid_grant` | `WARN auth.userinfo_401` | No |
| Unverified email claim | 403, "Ask an admin to verify your address." | `WARN auth.unverified_email` | No |
| Refresh `invalid_grant`, `refresh_token_obtained_at` ≥ 7 days old | Banner naming the 7-day cause + working reconnect link. **This is the expected weekly event, not an incident.** | `WARN auth.refresh_expired_testing_mode` + audit `auth.refresh_revoked` | No. Never auto-retried. |
| Refresh `invalid_grant`, token younger than 7 days | Banner: access was revoked at the Google account. Same reconnect route. | `WARN auth.refresh_revoked` + audit | No |
| Refresh 5xx / network | Sync shows "temporarily unavailable", previous data visible | `ERROR auth.refresh_transient` | 2s, 8s, then `next_attempt_at = now()+15m`. **Does not disconnect.** |
| Missing refresh token on callback | Nothing. Existing token and `refresh_token_obtained_at` preserved. | `DEBUG` | n/a |
| Day-5 expiry proximity | Proactive banner: "expires in 2 days — reconnect now" | `INFO auth.token_expiring_soon` | n/a. Informational. |

### 9.3 Calendar

| Failure | User sees | Logged | Retried |
| --- | --- | --- | --- |
| 403 `insufficientScope` | "Calendar access is missing. Reconnect Google." | `WARN calendar.insufficient_scope` + audit. `source.status='needs_reauth'`. | No. Needs user action. |
| 410 syncToken invalid | Nothing visible | `WARN calendar.sync_token_expired` | Cursor nulled, full resync on the next tick. Once. |
| 429 rateLimitExceeded | "Sync is backing off." | `WARN calendar.rate_limited` | `Retry-After` honored verbatim. `next_attempt_at` deferred. **Partial batches are not committed.** |
| 5xx / network | Previous calendar data stays visible | `ERROR calendar.sync_failed` | Exponential 1m, 5m, 30m. Max 3, then `status='error'`, admin-visible. |
| Deleted event | Vanishes from board | `DEBUG` | Upsert-by-ID makes deletion idempotent. |

**Invariant: a failed sync never deletes.** Rows are removed only after a *successful* full fetch of the window. A partial failure leaves the previous window intact. This is the difference between "calendar is 10 minutes stale" and "everyone's week is blank".

### 9.4 Gmail

| Failure | User sees | Logged | Retried |
| --- | --- | --- | --- |
| 403 `insufficientScope` | "Gmail access is missing. Reconnect." | `WARN gmail.insufficient_scope` + audit | No |
| 410 history invalid | Nothing visible | `WARN gmail.history_expired` | Cursor nulled, 30-day re-seed on next tick. Once. |
| 429 | "Inbox sync is backing off." | `WARN gmail.rate_limited` | `Retry-After` honored. No partial commits. |
| 5xx / network | Existing feed stays visible | `ERROR gmail.sync_failed` | 1m, 5m, 30m, then `status='error'` |
| **Send fails, 4xx** | Inline error in the reply box. **Composed text preserved.** | `WARN gmail.send_rejected` + status code. No body, no token. | No. |
| **Send times out, status unknown** | "We could not confirm your reply sent. Check the thread before sending again." | `ERROR gmail.send_unknown` | **Never blind-retried.** The user decides. A duplicate email is worse than an unconfirmed one. |
| Send 5xx before any bytes accepted | Same as timeout | `ERROR gmail.send_5xx` | Once, 2s, then stop |

The send path is the only place in the app where an error can produce an externally visible, hard-to-undo effect. It gets the most conservative treatment: idempotency key checked before send, nothing marked as sent unless Google returned success, and the user's draft never discarded on failure.

### 9.5 What is deliberately not done

No circuit breaker, no dead-letter queue, no background job framework, no distributed tracing, no retry library. Each sync command runs as its own process; backoff state lives in `source.next_attempt_at` in Postgres. When the sync volume justifies more, that is when it gets built.

---

## 10. Testing Strategy

### The honest ratio

This app is roughly 70% glue (views, templates, forms, ORM plumbing) and 30% logic that can be silently wrong (the availability algorithm, permission checks, token lifecycle, thread normalization). **Test the 30% hard and skip most of the 70%.** A line-coverage target would push effort into exactly the wrong places. The goal is not a percentage; the goal is that **every row of the §7 permission table has an assertion**, and that the §5 algorithm cannot regress without a red test.

Roughly 90% of test lines go to the areas below; the remaining 10% is one smoke test that the app boots, `/healthz` is 200, and one board page renders with a seeded user. The token-lifecycle and permission suites grew relative to the first draft of this spec, because two CEO decisions changed what is load-bearing: Testing mode makes the 7-day expiry a permanent operating state, and Model B makes the assignee a real authorization boundary rather than a label.

### Worth testing

**1. `tests/test_availability.py` — the algorithm. Highest value in the project.**
Table-driven over the §5 worked example plus the boundary cases that are cheap to test here and expensive to find in use:

| Case | Assertion |
| --- | --- |
| Adjacent events (`end == next.start`) | Merge into one block; produce no zero-length gap |
| Three-deep overlapping chain | Merges into a single block with the outermost bounds |
| Fully nested event | Merges; outer bounds win |
| Event entirely outside the window | Clipped away, no effect |
| Event straddling a window edge | Clipped to the edge |
| `is_busy = false` event | Excluded from merging entirely (§5.2 step 1) |
| Cancelled event | Excluded |
| Gap exactly equal to `duration_minutes` | **Emitted** |
| Gap one minute short | Not emitted |
| Free window before the first busy block | Emitted — catches an implementation that starts the cursor at the first conflict instead of the window start |
| 15-min grid on a gap starting at 09:07 | First candidate 09:15, never 09:07 |
| Event ending exactly at 17:00 | Business hours are `[09:00, 17:00)`, ending at 17:00 is inside it |
| Saturday / Sunday | No candidates generated |
| All-day event spanning two days | Treated as one continuous busy block across both days |
| Cross-midnight event (23:00–01:00) | Busy on both days; no candidate spans midnight |
| DST spring-forward (23-hour local day) | Candidates land on the local clock, not a fixed UTC offset |
| DST fall-back (25-hour local day) | No duplicate candidate for the repeated local hour |
| Two owners busy at the same time | One unioned block, not two |
| Nothing fits | Returns `[]` **with** a reason — never an empty list without one |
| Window in the past | No candidates, `no_window` reason |

~20 cases, pure functions, no fixtures, no database, milliseconds to run. This file is the reason the algorithm lives in its own module with no Django imports — it should be importable without `django.setup()`.

**2. `tests/test_permissions.py` — the security risk.**
Driven by the §7 table itself: a data structure mirroring the table, one generated test per row asserting allow/deny. A row added to the spec without a matching entry here is a review finding. Row 8 needs a dedicated matrix because it is the only non-trivial predicate:

| Scenario | Expected |
| --- | --- |
| Admin edits a task assigned to another member | 200, changed |
| Assignee edits their own task | 200, changed |
| Member edits a task assigned to a colleague | **403, zero rows changed** |
| Member claims an unassigned task by assigning it to themselves | 200, changed |
| Member sets an unassigned task to a third person's id | **403** (8a) |
| Assignee hands their own task to someone else | 200, changed |
| Member tries to reassign a colleague's task to themselves | **403** — the pre-edit assignee check, and the case that catches a read-then-check implementation |
| Member reads a task assigned to a colleague | 200 (read is shared, row 6) |
| Member DELETE on any task | 403 |
| Audit read as member | 403 |
| Member GETs another user's `message` | 404 |
| Client-supplied `created_by_id` on create | ignored, `actor.id` used |
| `/admin/` as member | 403 |
| `audit_log` UPDATE / DELETE | no-op at the database level |
| Admin GETs another user's mailbox | 404 — row 16 denies admin too, and 404 not 403 |

Also asserts a member requesting an admin-only route gets 403 and not 302.

**3. `tests/test_token_lifecycle.py` — the highest-risk integration.**
Token endpoint faked, never a network call. Covers:

*Encryption and redaction:*
- encrypt/decrypt round-trip; a tampered ciphertext fails to decrypt (AES-GCM must reject it, not return garbage); a wrong key fails closed with a clear log line, never a silent empty string; the redaction filter drops every secret-keyed field from a synthetic log record.

*Refresh:*
- refresh fires when `token_expires_at` is under 60s away; refresh does **not** fire when it is comfortably in the future (a no-op call is a wasted round-trip and a wasted quota unit); a missing `refresh_token` in a callback response preserves the stored one **and** leaves `refresh_token_obtained_at` untouched; a `5xx` refresh does **not** disconnect.

*Testing-mode expiry — mandatory, because it is the design target (§4.6), not an edge case:*
- a token whose `refresh_token_obtained_at` is **older than 7 days** and which returns `invalid_grant` sets `needs_reauth` + `disconnected_at` + `last_error_code='invalid_grant'`, shows a banner that names the 7-day cause rather than a generic error, **and preserves all task and message rows**;
- the same expired token produces **no crash and no silent stale feed** — the user is visibly disconnected, not quietly shown stale data;
- a token **younger than 7 days** returning `invalid_grant` is reported as a revocation, not as an expiry (the two causes are distinguishable, which is the point of the column);
- the day-5 sweep flags a user approaching expiry and the banner links to a working reconnect route;
- the reconnect route, exercised end to end, restores `active` status and sets a fresh `refresh_token_obtained_at`;
- a source in `needs_reauth` is **skipped** by the scheduler rather than retried.

**4. `tests/test_normalize.py`.**
`DISTINCT ON` thread collapse returns one row per thread with the latest message; unread-any aggregation; `In-Reply-To`/`References` built from a seeded 4-message thread match the expected header strings; `idempotency_key` uniqueness makes a second send with the same key a no-op; a send that raises leaves no `direction='outbound'` row.

**5. `tests/test_seed.py`.**
Seed is idempotent — run twice, row counts identical. Seed refuses to run with `OPSHUB_ENV=production`. `source='seed'` rows never collide with a real `google_sub`.

### Not worth testing

View and template rendering. CSS. Task CRUD plumbing — Django's ORM and forms are tested upstream. The Google API client libraries. Seed data *content* (that it says "vendor contract", not that it is spelled correctly). "Endpoint returns 200" smoke tests beyond the single app-boots check. Browser/E2E tests in phase 1 — no SPA, so the server-side tests cover the behavior that matters.

---

## 11. Risks, Signals, Rollback

### 11.1 Accepted risk register

| # | Risk | Likelihood | Blast radius | Signal that it is happening | Mitigation |
| --- | --- | --- | --- | --- | --- |
| 1 | **Consent screen is in Testing mode and stays there.** Refresh tokens expire after 7 days; **every user's sync breaks weekly, indefinitely.** No Workspace org exists, so there is no date on which this stops by itself. | Certain — it is the design target, not a risk to be retired | Schedule slip and chronic friction. Not data loss: disconnected users keep readable stale data | `source.status` count climbing to `needs_reauth` on a 7-day cadence, one per user per week; `refresh_token_obtained_at` clustering 7 days before each | Built as the operating state: `refresh_token_obtained_at` tracked, day-5 proactive banner, `needs_reauth` skipped by the scheduler, explicit 7-day test in `test_token_lifecycle.py`, reconnect UX as a first-class route. Flipping to Internal is go-live gate **G1**. Risk mitigated by design, mitigated further by G1. |
| 2 | **Gmail/Calendar scope choice cascades into the data model.** Asking for `gmail.modify` later means re-consenting every user; not asking now and needing it later means a schema migration. | Resolved — low going forward | Re-consent campaign if a future feature needs a wider scope | Any feature request needing `gmail.modify`, `calendar` (write), or `drive` | Decided before schema freeze: `gmail.readonly` (local unread state), `gmail.send`, `calendar.readonly`. All three are read-only or send-only, matching exactly what phase 1 does. Adding calendar write later requires a full re-consent round-trip (§4.1) and is not approved for phase 1. |
| 3 | **Shared task list with 2 roles means every read needs a permission check.** Miss one and a member sees data they should not — and under Model B a missed *write* check lets a member edit a colleague's task. | Medium, raised by Model B | Security incident | Any `task` readable by a non-member; any task edited by someone who is not the assignee; any audit row referencing a user outside the team | Structural enforcement, not discipline: views hold no ORM queries, services take the actor, permission predicates live in the SQL `WHERE`, CI fails on `objects.` outside the services layer, and §10 carries a 15-case matrix for row 8/8a alone. |
| 4 | **OAuth tokens in logs or committed config.** | Low (control in place) | Full account compromise for every team member | Secret scanner alert; any token substring in any log line; any token in git history | AES-256-GCM at rest; key from env only, never in DB or code; tokens never logged (root-logger redaction filter, unit-tested); `.env.example` holds names only; tokens exist in plaintext only inside two functions. |
| 5 | **Open signup reaches real users.** Phase 1 allows any Google account to become a member. If that posture is still live when a real user signs in, anyone can read the team task board. | Low if gates are honoured; **certain if G2 is skipped** | Unauthorized read of the board; pollutes the audit log | An unknown address appears in the audit log or directory | Go-live gate **G2**: flip to invite-only before the first real onboarding. The code path (claim-or-reject) is built and tested in phase 1; the gate is running it, not writing it. |

### 11.2 Additional risks found while writing this spec

| # | Risk | Likelihood | Signal | Mitigation |
| --- | --- | --- | --- | --- |
| 7 | **Sync volume / Gmail API quota.** Shared Workspace domains draw from a 1,000,000-unit/day quota. A full re-sync for 5 users plus polling could approach it as the team grows. | Low at 5 users, Medium at 50 | `quotaExceeded` in logs; `source.status='error'` cluster | Incremental `history.list` by default, 30-day window on cold start, 100-message batches, per-user cursor. Ceiling accepted: ~50 users on one domain. Beyond that, escalate to a second OAuth client or a second Workspace domain — a System Architect decision, not a code change. |
| 8 | **Timezone and DST bugs in availability.** A 23-hour or 25-hour local day produces wrong free slots. | Medium | Slots offered outside real business hours, missing entirely, or duplicated on a DST day | §5.1 UTC-everywhere rule; spring-forward and fall-back cases in `test_availability.py` (§10). |
| 9 | **Duplicate sends on ambiguous timeout.** | Low | Two identical emails in a thread | Idempotency key checked before send (§6.3), unique index, and no blind retry on unknown status (§9.4). |
| 10 | **HTML mail content.** Stored `body_html` is an XSS vector if ever rendered. | Low in phase 1 | — | Phase 1 does not render `body_html` at all. If it is ever rendered: sandboxed iframe, no script execution, strict CSP. |
| 11 | **A disconnected user works from stale data without noticing.** Under Testing mode this is the default weekly state for every user. | **High** — it is what the 7-day cycle produces | Wrong decisions made on a week-old inbox and calendar | The feed renders while `disconnected_at IS NOT NULL`; nobody reconnected within 7 days | Never design for silent staleness: persistent non-dismissible banner, stale-data labels with the data's age, day-5 proactive warning, `needs_reauth` sources skipped rather than retried (§4.5). Test asserts the disconnected state is visible, not merely recorded. |
| 12 | **Model B leaves unclaimable or stuck tasks.** A task assigned to someone on leave cannot be moved by a member, and (without the carve-out) an unassigned task could belong to nobody. | Medium | Work items silently stall | Tasks in `in_progress` with no activity for two weeks; unassigned `todo` items | Unassigned tasks are claimable by any member (§7 row 8 carve-out). Moving a colleague's task requires an admin — accepted, since on a 5-person team an admin is always reachable, and the audit log records the intervention. |

### 11.3 Rollback

**Phase 1 schema changes are additive only.** Every table is new. Every index is created alongside, never in place of, an existing one. Therefore rollback of the application is just redeploying the previous image — the previous code runs correctly against the new schema, because the new schema is a strict superset.

- **Application rollback:** redeploy the previous image tag. No data action. Seconds.
- **Schema rollback:** not required for phase 1, and this is by design. If a future migration is destructive, the rollback artifact is a named, timestamped `pg_dump` taken immediately before that migration and stored off-instance. The migration runner refuses to apply a destructive migration without an explicit `OPSHUB_ALLOW_DESTRUCTIVE=1` env var, which is a deliberate speed bump requiring someone to have thought about it.
- **No data-destructive step is applied without written CEO approval.** `DROP TABLE`, `TRUNCATE`, `DELETE` without a `WHERE`, dropping the `audit_log` immutability rules, and any migration touching tokens or `audit_log` contents all fall in this category.
- **Data already collected (tasks, cached mail, calendar events) is never destroyed by a failed deploy or a failed sync.** Sync failure preserves the previous window; disconnect preserves everything and marks it stale.

---

## Appendix A — Open dependencies owned outside engineering

| # | Dependency | Owner | Blocks |
| --- | --- | --- | --- |
| 1 | **No Google Workspace organization exists.** Consent screen is Testing and stays Testing until gate **G1**. | CEO | Nothing in phase 1. It blocks only the Internal flip, not development, verification, or delivery. The 7-day re-auth cycle is the permanent operating model until then. |
| 2 | Google Workspace domain, for gate G1 | CEO | Flipping to Internal. Owner has no date; engineering is not blocked. |
| 3 | A Google Cloud project and OAuth client, with the project owner accepting the unverified-app warning emails | CEO | First sign-in. |
| 4 | 5+ real accounts added as **test users** in the Google Cloud console | CEO | Manual QA with real mail and real calendars. Nobody can sign in in Testing mode without being added first (gate G3). |
| 5 | `OPSHUB_TOKEN_KEY` generation and storage in the deploy platform's secret manager | CEO / platform | Production deploy (gate G4). Engineering must never handle the raw key value. |

## Appendix B — Deliberate deferrals, with their ceilings

| Deferred | Ceiling accepted | Upgrade trigger |
| --- | --- | --- |
| Real-time sync | Polling every 5 minutes; data can be up to 5 minutes stale | A user acts on staleness and is wrong |
| Multi-tenancy | Single workspace, no `workspace_id` | A second team needs isolation |
| Search | None. Filtering is status/priority/assignee/due-date only | Someone asks for full-text search |
| Task comments / attachments | None | A task needs more than a description |
| Bulk task operations | None beyond admin bulk delete | — |
| Cross-owner "double-booked people" reporting | Not shown; §5.3 is per-person only | A lead asks "who is double-booked with whom" |
| Background job framework | Scheduler-driven management commands with `next_attempt_at` backoff in Postgres | Sync runtime exceeds the command timeout |
| Invite-only signup | Phase 1 is open signup by decision | Go-live gate **G2**, before any real user. The code path is built in phase 1; only the flag flips. |
| Calendar write (booking a suggested slot) | Not requested, not implemented | A feature needs it. Costs a full re-consent round-trip for every user (§4.1). Not approved for phase 1. |
| Per-user business hours | One 09:00–17:00 Mon–Fri constant, evaluated in the requester's timezone | A team member outside those hours asks for their own |
| Token expiry countdown in the UI | A day-5 banner, not a live countdown | Someone asks how long they have before being disconnected |

Each of these is a small, local change against an existing seam — no redesign. That is the test a deferral has to pass to be worth making.
