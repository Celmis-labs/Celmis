# ee/ — the commercial boundary

Everything under an `ee/` path — this directory, `src/ee/`, `web/ee/`,
`tests/ee/` — and every file anywhere whose name contains `.ee.`, is covered by
[`LICENSE_EE`](../LICENSE_EE) rather than by the AGPL that covers the rest of
Celmis.

The official build, run without a licence, admits up to five people; a licence
lifts that (see [Seats and the community limits](#seats-and-the-community-limits)).

**This directory itself holds documentation only.** The code lives next to the
code it extends, because that is where each build looks for it: the API image
copies `src/` and nothing else, and the web build resolves `@/ee/...` inside
`web/`.

## Why the line was drawn before there was anything behind it

The boundary cost an hour. Adding it after the first outside contribution
would have cost a conversation with every contributor who had already sent
work under an unqualified AGPL — because a contribution arrives under the
licence it was made under, and no later file can retroactively change that.

So the line went in before the first tag, and stayed empty until there was
something to put behind it.

## What is here

New **enterprise** capabilities, from their first commit. Things a company
buys because it is a company. Two today:

| Feature | Licence name | API | Web | Tests |
|---|---|---|---|---|
| Single sign-on (OIDC / Keycloak); up to five people without a licence | `sso` | `src/ee/sso/` — `POST /api/auth/oidc` | `web/ee/sso/` — the NextAuth provider and the "Sign in with …" button | `tests/ee/test_oidc_sign_in.py` |
| Review analytics ("extended reporting") | `analytics` | `src/ee/analytics/` — `GET /api/analytics/summary` | `web/ee/analytics/` — the `/analytics` dashboard and charts | `tests/ee/test_analytics_is_a_leads_view.py` |

The licence check is `src/ee/license.py`; the mount point is
`src/ee/__init__.py`.

What these features build on stays AGPL, because a lapsed licence must not
take it away: the users store's `oidc_iss`/`oidc_sub` columns and
`UserAuthMethod.OIDC` (an SSO user must stay readable), the master-account
guard, `AUTH_PASSWORD_LOGIN`, the editor role and `require_analytics_access`
(RBAC); the review issues and pull-request tables and everything that writes
them; the ignore globs and the comment threshold. Analytics only reads.

## What does not

Anything a self-hosting team needs to run Celmis honestly. That includes every
capability shipped at the first public release, and in particular:

| Stays AGPL | Why |
|---|---|
| `src/api/routers/audit.py` | The audit console. Taking it away hurts exactly the audience AGPL is chosen for, and earns nothing while there is no key to sell. |
| `src/security/audit.py` | The audit **log**. A security control, never enterprise-only — the trail is always written. |
| `src/access/resolver.py` | Reads as RBAC, is actually the data boundary between teams for Q&A, the graph and vectors. Behind a key, the free build leaks between tenants. |
| `src/security/redactor.py`, `egress.py`, `log_filter.py` | Defensive controls. |
| `src/api/routers/gdpr.py` | A legal obligation of the European user, not a premium. |
| `src/api/routers/teams.py` | Currently the basis of workspace separation. Not to be touched until "who sees what" is split from "who administers what". |

When in doubt: if switching it off would make the free build **less safe** or
**less correct**, it is not an enterprise feature.

## The licence check

A licence is a signed offline token: a JWT with `alg: EdDSA` (Ed25519),
verified at start-up against the public key embedded in
`src/ee/license.py`. Self-hosting has to work in a closed network, so it never
calls home. Its claims:

| Claim | Meaning |
|---|---|
| `iss` | always `celmis-licensing` |
| `sub` | the customer, as shown to an administrator |
| `features` | list of licence names: `sso`, `analytics` |
| `iat`, `exp` | required; `nbf` optional |
| `seats` | optional whole number from 1 to 1,000,000: how many people the installation may hold. Absent = **unlimited**, which is what every licence issued before seats existed means |
| `sso_seats` | optional whole number from 1 to 1,000,000: how many people may sign in through single sign-on, on a licence that grants `sso`. Absent = **unlimited** |

A `seats` or `sso_seats` that is **present but malformed** (zero, negative,
text, a fraction, a boolean, `null`, above the ceiling) is read as the
**community limit** of five — it fails closed, never to "unlimited" — and the
log names the claim at ERROR level (never the token or the value). The licence
itself stays valid, so its features keep working: refusing it would take away
what the customer paid for. A licence **never gives fewer than the free
allowance**: the limit in force is the larger of the claim and five, so
`--seats 3` is five. See
[Seats and the community limits](#seats-and-the-community-limits).

**How it is applied.** `src/api/main.py` makes one guarded call —
`from src.ee import mount_enterprise` inside `try/except ImportError` — and
that is the only import of `src.ee` anywhere in the AGPL code
(`tests/api/test_the_community_build_needs_no_ee.py` reads the import graph to
keep it so). `mount_enterprise` verifies the licence and mounts the router of
each feature it grants, and only those — plus the licence router
(`src/ee/license_router.py`, `/api/license`), always, because that is how a
community installation becomes an enterprise one. It is the first call of
`apply_license`, which is idempotent and runs again whenever the licence is
changed from the UI. A missing, unreadable, forged,
tampered, wrong-algorithm, wrong-issuer, not-yet-valid or expired licence
mounts no licensed feature: the process starts as the **community edition**
and logs why —
a `WARNING` naming the reason (never the token) for a licence that was given
and failed, an `INFO` line when none was given at all.

A process can outlive its licence, so every enterprise route also re-checks the
licence in force per request and answers 403 once it has expired, been removed
or been replaced by one that does not grant the feature. Restarting after
expiry unmounts them; removing or replacing it from the UI unmounts them at
once. The one exception is single sign-on, which stays mounted without a
licence for up to five people (the SSO allowance, below): what a missing or
lapsed licence changes there is the *limit*, never who is signed in.

**What the rest of the product sees.** `/api/capabilities` reads availability
from the route table, so an unmounted feature reports `available: false` with
no list to keep in step, and the frontend hides the Analytics tab by itself
(the SSO button follows the web's own `AUTH_OIDC_*` configuration, because the
endpoint is always there). The same document reports `edition` (`community` |
`enterprise`) and a `license` summary — the granted features for everyone, the
customer and expiry only to a signed-in caller; the token is never on it.
`/admin/health` shows it as the Edition card.
`tests/api/test_capabilities_says_what_is_mounted.py` runs in both editions
and turns the build red if the map and the route table ever disagree.

Note what the check is *not*: `capabilities.py` says so itself, and it must
stay true — a licence decides whether a route is **mounted**. It is not an
authorisation boundary. Every endpoint still does its own 401 and 403.

## Installing a licence

Two ways, and the environment wins when both are used.

**From the UI (no restart).** Sign in as a global admin, open
**Admin → Health**, paste the key into the Edition card and press
**Activate**. The API verifies it (`PUT /api/license`); a key that does not
verify — wrong signature, expired, not yet valid, wrong issuer, granting no
feature this build has, or far too large — is refused with the reason and
nothing is stored. A valid one is stored encrypted in the credential store
(`src/ee/license_store.py`: an installation-wide slot, not a workspace one, in
the workspace volume — it survives restarts and container rebuilds) and
applied **at once**: the features it grants are mounted, `/api/capabilities`
reports `enterprise` on the next request, and the Analytics tab appears (and
the seat limits change) without a reload. **Remove licence** (`DELETE /api/license`)
takes them away just as fast: their routes are unmounted, and every
enterprise route re-checks the licence in force per request, so a request
already in flight is refused with 403. A *new* request to an unmounted route
gets **404**, not 403 — deliberately: it is what a restart without the licence
gives, and it keeps `/api/capabilities` (which reads the route table) telling
the truth. Replacing a licence with one that drops
a feature does the same for that feature. Saves and removals are serialised,
so two admins acting at once cannot leave one licence in force while another,
or none, is stored. If the stored licence cannot be read at start-up (a
changed master key, a locked store), the card says so
(`stored licence unreadable`) instead of looking like an installation that
never had one; restart once the store is readable, or paste the key again. Each save, replacement and removal
is an audit row (`license.saved` / `license.replaced` / `license.removed`)
naming the admin, the customer, the features and the expiry — never the key.

**From the environment.** Give the **API** container one of:

```bash
CELMIS_LICENSE_KEY=eyJhbGciOiJFZERTQSIs...        # the token itself
CELMIS_LICENSE_FILE=/workspace/data/celmis.license  # or a file holding it
```

A file must be somewhere the API container can see. The shipped
`docker-compose.yml` mounts only the `workspace_data` volume at
`/workspace/data` (and the vault), so put the file there — a path like
`/run/secrets/...` works only if you add that mount or secret yourself.

**Precedence:** `CELMIS_LICENSE_KEY` > `CELMIS_LICENSE_FILE` > the key entered
in the UI. While either variable is set the licence is *managed by the server
environment*: the Edition card shows it read-only, and `PUT`/`DELETE
/api/license` answer 409 rather than store a key the variable would silently
shadow. The variable wins even when what it holds does not verify. Restart
the API after changing it; the log says `license_valid source=env_key
customer=... features=... expires_at=...`, and `/admin/health` shows the
edition. SSO additionally needs the `AUTH_OIDC_*` variables, which the shipped
compose forwards to both the web and the API container — see `.env.example`.
(The API also accepts bare `OIDC_ISSUER` / `OIDC_CLIENT_ID`, but compose does
not forward those names, so under it they never arrive.)

A UI change applies to the API process that received it. A deployment running
several API replicas picks it up on the others at their next restart; the
shipped compose runs one.

## Minting a licence

```bash
python scripts/ee_mint_license.py --customer "Acme Corp" \
    --features sso,analytics --days 365 \
    --key ~/.celmis-ee/license-signing-key.pem
```

Add `--seats 50` to limit the people the installation may hold and
`--sso-seats 25` to limit those who sign in through single sign-on; leave a
flag out for "no limit". The script refuses a value the verifier would read as
malformed: anything but a whole number from 1 to 1,000,000 (a fraction or a
boolean is refused, not rounded).

The command prints the token on stdout. The private key never enters this repository;
only its public half is in `src/ee/license.py`. The script is AGPL and anyone
can run it — a token signed with any other key does not verify. Tests sign
with a throwaway keypair (`tests/ee/licensing.py`) and point the embedded key
at it for the duration of the test; one test asserts that such a token does
**not** verify against the real key.

## Seats and the community limits

Without a licence an installation is the **community edition** and holds
**up to five people**; a licence lifts that. The community rule and the count
are `src/users/seats.py` (AGPL; it does not import `src/ee`, which only tells
it what the licence in force allows). The single sign-on allowance is counted
and enforced by `src/ee/sso/allowance.py`, a covered file, which registers
itself with the AGPL module as an extension: every way of adding or switching on
a person asks it, so it is the one check, not a second copy of it.

| | Without a licence | With a licence |
|---|---|---|
| People (seats) | 5 | `seats` claim, at least 5; absent = unlimited |
| People signing in through single sign-on | 5 (the SSO allowance) | `sso_seats` claim on a licence that grants `sso`, at least 5; absent = unlimited. A licence that does not grant `sso` leaves the allowance of 5 |

- **A seat** is an active account that is not platform-owned (the master
  break-glass account and the seeded local user take none). An *SSO seat* is a
  seat bound to the identity provider.
- **Only adding a person is refused**: a sign-up, a first Google sign-in, a
  first single sign-on sign-in, binding an existing local account to single
  sign-on (it takes an SSO seat, not a new seat), switching a deactivated
  account on again, `analyzer auth google` for a new address. The answer is
  `403` with
  `{"code": "seat_limit_reached" | "sso_seat_limit_reached", "message": ...}`.
  The message names the **limit** ("reached its limit of 5 people") and never
  the head-count, because the caller is not signed in; the exact counts are on
  `/api/admin/seats` and in the one `auth.seat_limit_refused` audit row, which
  never carries the address. Nobody who already has an account is ever
  refused or switched off by the software — not when the installation is above
  the limit, not when a licence is removed or lapses.
- **Switching an account off and on** frees and takes a seat. **Admin → Users**
  (the master account only; `POST /api/admin/users/{id}/deactivate` and
  `/reactivate`) and `analyzer auth deactivate|reactivate EMAIL`. A deactivated
  account is refused on every way in at once — the session and its refresh, the
  password, Google, single sign-on, MCP tokens (the `full` and `dev` profiles)
  and pack tokens — and nothing is deleted. The master, the local default
  account and the account holding the master address cannot be switched off.
  Reactivating goes through the same seat check as a sign-up. Each change leaves
  an `auth.user_deactivated` / `auth.user_reactivated` audit row naming the
  person by id only.
- **The command line reads the licence.** `analyzer auth ...` mounts no app, so
  before it adds or switches on a person it loads the configured licence the
  same guarded way `src/api/main.py` does (`src.ee.publish_seat_limits`, inside
  `try/except ImportError`); a licensed installation is not held to five there.
- **The SSO allowance** is the exception in section 2 of
  [`LICENSE_EE`](../LICENSE_EE): single sign-on may be used in production or
  for internal use without a commercial licence while no more than five people
  are bound to it. The check that counts them is `src/ee/sso/allowance.py`, a
  licence check; do not remove it. That the software switches nobody off when a
  licence lapses does not remove the requirement of a commercial licence beyond
  five people.
- **Where to see it**: **Admin → Health → Seats** (`GET /api/admin/seats`):
  used and limit for seats and, in a build that has single sign-on, for single
  sign-on; "unlimited" when the licence sets none, and the community rule when
  there is no licence. `GET /api/invites/seats` (a yes or no for the invite
  dialog) answers only people who may invite into a team workspace; in
  somebody's personal workspace, which everybody owns, it is a 403.
- **Concurrency**: the check and the creation happen under one lock and the
  people are counted again afterwards, so racing requests in one API process
  cannot pass the limit. Several API processes sharing one users database could
  each pass the check before either creates; the re-count undoes the overshoot
  it sees, but this is not a distributed lock. The shipped compose runs one API
  process.
