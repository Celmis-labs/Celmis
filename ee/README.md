# ee/ — the commercial boundary

Everything under an `ee/` path — this directory, `src/ee/`, `web/ee/`,
`tests/ee/` — and every file anywhere whose name contains `.ee.`, is covered by
[`LICENSE_EE`](../LICENSE_EE) rather than by the AGPL that covers the rest of
Celmis.

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
| Single sign-on (OIDC / Keycloak) | `sso` | `src/ee/sso/` — `POST /api/auth/oidc` | `web/ee/sso/` — the NextAuth provider and the "Sign in with …" button | `tests/ee/test_oidc_sign_in.py` |
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

**How it is applied.** `src/api/main.py` makes one guarded call —
`from src.ee import mount_enterprise` inside `try/except ImportError` — and
that is the only import of `src.ee` anywhere in the AGPL code
(`tests/api/test_the_community_build_needs_no_ee.py` reads the import graph to
keep it so). `mount_enterprise` verifies the licence and mounts the router of
each feature it grants, and only those. A missing, unreadable, forged,
tampered, wrong-algorithm, wrong-issuer, not-yet-valid or expired licence
mounts nothing: the process starts as the **community edition** and logs why —
a `WARNING` naming the reason (never the token) for a licence that was given
and failed, an `INFO` line when none was given at all.

A process can outlive its licence, so every enterprise route also re-checks the
expiry per request and answers 403 once it has passed. Restarting after expiry
unmounts them.

**What the rest of the product sees.** `/api/capabilities` reads availability
from the route table, so an unmounted feature reports `available: false` with
no list to keep in step, and the frontend hides the Analytics tab and the SSO
button by itself. The same document reports `edition` (`community` |
`enterprise`) and a `license` summary — the granted features for everyone, the
customer and expiry only to a signed-in caller; the token is never on it.
`/admin/health` shows it as the Edition card.
`tests/api/test_capabilities_says_what_is_mounted.py` runs in both editions
and turns the build red if the map and the route table ever disagree.

Note what the check is *not*: `capabilities.py` says so itself, and it must
stay true — a licence decides whether a route is **mounted**. It is not an
authorisation boundary. Every endpoint still does its own 401 and 403.

## Configuring a licence

Give the **API** container one of:

```bash
CELMIS_LICENSE_KEY=eyJhbGciOiJFZERTQSIs...   # the token itself
CELMIS_LICENSE_FILE=/run/secrets/celmis.license   # or a file holding it
```

The variable wins when both are set. Restart the API; the log says
`license_valid customer=... features=... expires_at=...`, and `/admin/health`
shows the edition. SSO additionally needs the `AUTH_OIDC_*` variables on the
web container and `OIDC_*` (or the same `AUTH_OIDC_*`) on the API — see
`.env.example`.

## Minting a licence

```bash
python scripts/ee_mint_license.py --customer "Acme Corp" \
    --features sso,analytics --days 365 \
    --key ~/.celmis-ee/license-signing-key.pem
```

prints the token on stdout. The private key never enters this repository;
only its public half is in `src/ee/license.py`. The script is AGPL and anyone
can run it — a token signed with any other key does not verify. Tests sign
with a throwaway keypair (`tests/ee/licensing.py`) and point the embedded key
at it for the duration of the test; one test asserts that such a token does
**not** verify against the real key.
