# Licensing

Celmis is **AGPL-3.0-or-later**, with one exception drawn by path.

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
    is kept and none can drift out of date. At the time of writing `ee/` holds
    no product code: the line exists before it is needed because adding it
    later would mean re-asking every contributor who had already sent work
    under an unqualified AGPL.

    Everything else in this repository — the whole of `src/`, `web/`,
    `tests/`, `scripts/` and the deployment files — is AGPL-3.0, including the
    audit console, the usage and spend views, the compliance checks and the
    installation metrics. Those are not held back: a team self-hosting Celmis
    is exactly the audience this licence is chosen for, and taking their audit
    console away on the first release would be a cost to them and no revenue
    to anybody.

    Section 13 of this licence applies to Celmis. It is a network service:
    anyone who interacts with a running instance over a network is entitled to
    its source. The application states its version and links to the source at
    that version in its own interface.
