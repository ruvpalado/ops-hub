# Go-live gates (G1–G5)

Copied from spec §4.6 with explicit owners and checkboxes. Each gate must be
checked by its owner before the next gate is opened. G2 is the one most likely
to be skipped — nothing about it is visible until an unknown address is sitting
in the audit log.

---

## G1 — Flip consent screen to Internal

**Owner:** CEO (requires Google Workspace domain)
**Status:** ☐ Not started

**What:** In Google Cloud Console → OAuth consent screen, change "User Type" from
**Testing** to **Internal** (requires a verified Workspace domain). This lifts the
7-day refresh-token expiry and stops the weekly forced-reconnect cycle.

**Why it matters:** Until G1, every user's token expires after 7 days (spec §4.6
row 1). The app is designed for this — the scheduler, banner, and reconnect
flow all assume weekly expiry. G1 is the *only* thing that changes the risk
profile from "certain weekly breakage" to "rare revocation".

**Verification:**
- [ ] Consent screen shows "Internal" in Google Cloud Console
- [ ] A fresh sign-in yields a refresh token that does **not** have a 7-day life
- [ ] The day-5 proactive banner (`auth.token_expiring_soon`) stops firing for
      new users
- [ ] `test_seven_day_expiry_banner_names_the_testing_mode_cause_not_a_generic_error`
      is marked `xfail` or deleted (it tests the testing-mode behaviour that G1
      removes)

**Rollback:** Flip back to Testing in Google Cloud Console. The app's
reconnect flow and weekly expiry logic remain fully functional.

---

## G2 — Flip signup to invite-only before any real user

**Owner:** Engineering runs, CEO verifies
**Status:** ☐ Not started

**What:** Set `OPSHUB_SIGNUP_INVITE_ONLY=true` (and `OPSHUB_ALLOW_SIGNUP=false`)
in the production environment *before* the first real (non-seeded) user signs in.

**Why it matters:** If an unknown address signs up and lands in the audit log
before invite-only is on, there is no automated way to distinguish them from a
legitimate invitee. The only signal is an unexpected row in `accounts_appuser`
and an `auth.signup` audit entry with no matching invite. G2 must happen
*before* the first real user, not after.

**Verification:**
- [ ] `OPSHUB_SIGNUP_INVITE_ONLY=true` and `OPSHUB_ALLOW_SIGNUP=false` are set
      in production secrets
- [ ] A fresh incognito visit to `/` redirects to Google, completes consent,
      and then shows "Signups are invite-only" instead of creating an account
- [ ] The seeded demo users (`*@example.test`) still work (they were created
      via `seed_demo`, not the signup flow)
- [ ] The audit log shows zero `auth.signup` entries for unknown addresses

**Rollback:** Flip the env vars back. Existing accounts are unaffected.

---

## G3 — Add real team members as test users

**Owner:** Engineering
**Status:** ☐ Not started

**What:** In Google Cloud Console → OAuth consent screen → Test users, add the
email addresses of the real team members who will use the app before G1.

**Why it matters:** In Testing mode, only listed test users can complete the
OAuth flow. G1 (Internal) removes this restriction, but until G1 happens,
every real user must be pre-listed.

**Verification:**
- [ ] All intended team members' emails are in the Test users list
- [ ] Each can complete the OAuth flow and land on the task board
- [ ] Their sources show `status=active` after first sync

**Rollback:** Remove from Test users list. They will be blocked at the consent
screen until re-added or G1 flips to Internal.

---

## G4 — Confirm `OPSHUB_TOKEN_KEY` is in the platform secret manager

**Owner:** Engineering
**Status:** ☐ Not started

**What:** The production `OPSHUB_TOKEN_KEY` (32 random bytes, base64) is stored
in the platform's secret manager (e.g. AWS Secrets Manager, GCP Secret Manager,
Fly secrets, Render env vars) and **not** in the repo, not in `.env`, not in
Docker images.

**Why it matters:** This key encrypts every Google refresh token at rest. If it
leaks, every stored token is compromised. If it is lost, every stored token is
unrecoverable — users must reconnect. The key is generated once with
`manage.py set_token_key --write-env` and must be copied to the secret manager
before the first production deploy.

**Verification:**
- [ ] `OPSHUB_TOKEN_KEY` exists in the secret manager with the correct value
- [ ] The production deployment injects it as an env var (not baked into the
      image)
- [ ] `manage.py check` passes in the production environment
- [ ] A test sign-in → sync → re-deploy cycle decrypts tokens correctly

**Rollback:** If the key is lost, there is no rollback — every user must
reconnect. If the key is leaked, rotate it immediately: generate a new one,
write to secret manager, redeploy, and all users must reconnect (the old
encrypted tokens become undecryptable).

---

## G5 — Run the full suite against Internal settings

**Owner:** Engineering
**Status:** ☐ Not started

**What:** After G1 is flipped, run the entire test suite against a staging
environment configured with `OPSHUB_ENV=production` and the Internal consent
screen (or a production-like Testing mode that mimics Internal — i.e. no
7-day expiry).

**Why it matters:** The test suite includes tests that assert the 7-day expiry
behaviour (e.g. `test_seven_day_expiry_banner_names_the_testing_mode_cause_not_a_generic_error`).
Those tests will fail or become meaningless after G1. G5 is the gate that
confirms the test suite has been updated for the post-G1 reality, or that the
relevant tests are marked `xfail`/deleted with a note pointing at G1.

**Verification:**
- [ ] Staging environment has `OPSHUB_ENV=production` and Internal consent
      screen (or a mock that omits the 7-day expiry)
- [ ] `uv run pytest -q` passes (or fails only on tests explicitly marked as
      testing-mode-only with a comment referencing G1)
- [ ] The smoke test (`tests/test_permissions.py -k smoke`) still passes
- [ ] All three CI greps (`CRYPTO_CHOKEPOINT_OK`, `TEMPLATES_CLEAN`,
      `VIEWS_HOLD_NO_QUERIES`) pass

**Rollback:** If a regression is found, revert the staging deploy. G1 remains
flipped in Google Cloud Console; the code rollback is independent.

---

## Gate dependencies

```
G1 (CEO) ──────────► G3 (Eng) ──┐
                                 │
G4 (Eng) ◄──────────────────────┘
                                 │
                                 ▼
                             G5 (Eng) ──► GO LIVE
                                 ▲
G2 (Eng+CEO) ────────────────────┘
```

- G1 and G4 can proceed in parallel.
- G3 requires G1 (or at least the consent screen change to be in flight).
- G2 must complete **before** the first real user signs up — it is a timing
  gate, not a dependency gate.
- G5 requires G1 (the environment must reflect the post-G1 state).

---

## Emergency contacts

| Gate | Owner | Contact |
|------|-------|---------|
| G1   | CEO   | [CEO contact] |
| G2   | Eng + CEO | [Eng lead], [CEO contact] |
| G3   | Eng   | [Eng lead] |
| G4   | Eng   | [Eng lead] |
| G5   | Eng   | [Eng lead] |