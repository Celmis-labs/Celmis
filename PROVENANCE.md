# Provenance

What this file records: the licence position of this repository and the origin
of the code in it. It is a statement of facts, not a claim of ownership over
anything it does not cover.

This repository begins at a single commit. Development happened before it, in
private, and that history is not published — nothing in it is required to
build, run, audit or fork what is here. Every dependency is declared, no
third-party source is vendored, and the licence below applies to the whole
tree except where it says otherwise.

## Rights — AGPL-3.0, with one exception

This repository is licensed under the **GNU Affero General Public License
v3.0** — see [`LICENSE`](LICENSE) for the full text.

One exception, drawn by path so that git shows it and no separate registry of
covered files can drift out of date:

> Anything under `ee/`, and any file whose name contains `.ee.`, is covered by
> [`LICENSE_EE`](LICENSE_EE) instead.

`ee/` holds no product code. The boundary was drawn before it was needed
because adding it after the first outside contribution means re-asking every
contributor who has already sent work under an unqualified AGPL — a
contribution arrives under the licence it was made under, and no later file
changes that retroactively.

Everything shipped at the first public release is AGPL, including the parts
that look commercial: the audit console, usage and spend, compliance checks,
installation metrics. Withholding those would cost a self-hosting team — the
audience this licence is chosen for — and earn nothing while there is no key
to sell. Security controls are never enterprise-only: the audit *log* is
written by `src/security/audit.py` under AGPL and always will be.

**§13 binds here.** Celmis is used through a browser, so everyone interacting
with a running instance over a network is entitled to its source. The
application states its version and links to the source **at that version** in
its own footer — not to the default branch, which is code nobody is running.

External contributions are accepted under the AGPL; see
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Third-party code

Dependencies are declared in `pyproject.toml` and `web/package.json` and carry
their own licences. No third-party source has been vendored into this tree.
