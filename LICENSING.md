# Licensing

Celmis is **AGPL-3.0-or-later**, with one exception drawn by path.

The official build, run without a licence, admits up to five people (see
"Seats" below); a licence lifts that.

The full, unmodified licence text is in [`LICENSE`](LICENSE). The terms for
the excepted files are in [`LICENSE_EE`](LICENSE_EE). This file explains the
boundary between them and why it is where it is.

It lives here rather than at the top of `LICENSE` because a licence file with
a preamble in front of it is not recognised as that licence: GitHub compares
the file against a reference text by similarity, and twenty-five lines of our
own prose ahead of the grant were enough for it to answer `NOASSERTION` —
which reads to a directory, a package index, or anyone filtering by licence as
"no licence at all". The words below are unchanged; only their address is.

---

Celmis is licensed under the GNU Affero General Public License v3.0, with one
exception.

    THE ee/ EXCEPTION
    -----------------
    Anything whose path contains `ee/`, and any file whose name contains
    `.ee.`, is NOT covered by this licence. Those files are governed by
    LICENSE_EE in the root of this repository.

    The boundary is visible to git, so no separate registry of covered files
    is kept and none can drift out of date. The line was drawn before any
    product code lived behind it, because adding it later would mean
    re-asking every contributor who had already sent work under an
    unqualified AGPL. It now covers single sign-on and review analytics, in
    `src/ee/`, `web/ee/` and `tests/ee/`.

    Everything else in this repository — `src/`, `web/` and `tests/` outside
    their `ee/` directories, `scripts/` and the deployment files — is
    AGPL-3.0, including the
    audit console, the usage and spend views, the compliance checks and the
    installation metrics. Those are not held back: a team self-hosting Celmis
    is exactly the audience this licence is chosen for, and taking their audit
    console away on the first release would be a cost to them and no revenue
    to anybody.

    Section 13 of this licence applies to Celmis. It is a network service:
    anyone who interacts with a running instance over a network is entitled to
    its source. The application states its version and links to the source at
    that version in its own interface.

---

## Seats, and the single sign-on allowance

An installation without a licence (the community edition) holds **up to five
people**. A licence lifts that limit.

- **A seat** is an active account that is not platform-owned: the master
  (break-glass) account and the seeded local user take none. Deactivating an
  account frees its seat; erasing one deactivates it. An administrator
  deactivates and reactivates an account from **Admin / Users** (the master
  account only) or with `analyzer auth deactivate|reactivate EMAIL`. Nothing is
  deleted, the person is refused at once on every way in (session, refresh,
  password, Google, single sign-on, MCP and pack tokens), and reactivating
  needs a free seat. Neither the master nor the local default account can be
  switched off.
- **Nobody is ever locked out by the limit.** It refuses *adding* a person: a
  sign-up, a first Google or single sign-on sign-in, `analyzer auth google` for
  a new address, switching a deactivated account on again. Everybody who
  already has an account keeps signing in, accepts invites and joins
  workspaces and teams, even when the installation is over the limit (it had
  more people when the limit arrived, or a licence ran out).
- **Single sign-on** (`src/ee/sso/`, covered by `LICENSE_EE`) may be used
  without a licence by up to **five people** (the "SSO allowance", the
  exception in section 2 of `LICENSE_EE`). The sixth is refused; the people
  already signed in keep working. A licence that grants `sso` lifts the limit.
  The software not switching anybody off when a licence lapses does not remove
  the requirement of a commercial licence for more than five people.
- **A licence** carries two optional claims, `seats` and `sso_seats` (see
  [`ee/README.md`](ee/README.md)). A claim that is absent is unlimited, so
  licences issued before seats existed keep working unchanged; a licence that
  grants `sso` without `sso_seats` is unlimited for single sign-on. A claim
  that is present but not a whole number from 1 to 1,000,000 is read as the
  free limit (five) and logged as an error, never as unlimited. A licence never
  gives fewer than the free allowance: the limit in force is the larger of the
  claim and five.
- **The command line** (`analyzer auth ...`) reads the configured licence the
  same way the API does before it adds or switches on a person, so a licensed
  installation is not held to five there.

Where the rule lives: the counting and the community limit of five are in
`src/users/seats.py` (AGPL, and it does not import `src/ee`). The single
sign-on allowance is counted and enforced by `src/ee/sso/allowance.py`, a
covered file, which plugs into the AGPL module through a registered hook so
that every way of adding or switching on a person asks it. The enterprise
licence code tells both what the licence in force allows. The numbers an
administrator sees are on **Admin / Health**.
