# ee/ — the commercial boundary

Everything under this directory, and every file anywhere whose name contains
`.ee.`, is covered by [`LICENSE_EE`](../LICENSE_EE) rather than by the AGPL
that covers the rest of Celmis.

**This directory is intentionally empty of product code.**

## Why draw a line around nothing

The boundary costs an hour now. Adding it after the first outside contribution
costs a conversation with every contributor who has already sent work under an
unqualified AGPL — because a contribution arrives under the licence it was made
under, and no later file can retroactively change that.

So the line goes in before the first tag, and stays empty until there is
something to put behind it.

## What goes here

New **enterprise** capabilities, from their first commit: SSO/SAML, an
organisation-wide role directory, extended reporting. Things a company buys
because it is a company.

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

## There is no licence check yet

None is written, and none should be until there is a first customer. The
mechanism is about half a day on top of the existing
`src/api/routers/capabilities.py`: do not mount the `ee/` routers without a
valid key, and `capabilities` stops advertising the prefixes, the frontend
hides the pages by itself, and
`tests/api/test_capabilities_says_what_is_mounted.py` turns the build red if
the two ever disagree.

Note what that mechanism is *not*: `capabilities.py` says so itself, and it
must stay true — a licence key decides whether a route is **mounted**. It is
not an authorisation boundary. Every endpoint still does its own 401 and 403.

The key, when it exists, is a signed offline token (Ed25519 JWT, public key in
the source, verified at start-up). Self-hosting has to work in a closed
network, so it must never call home.
