# Contributing to Celmis

Thanks for looking. This document is short and covers the two things that are
easy to get wrong: **where new code goes**, and **what a test here is expected
to prove**.

## Licence, and the one rule about it

Celmis is **AGPL-3.0**. By sending a change you agree it is contributed under
that licence.

There is exactly one exception, and it is drawn by path:

> Anything under `ee/`, and any file whose name contains `.ee.`, is covered by
> [`LICENSE_EE`](LICENSE_EE), not by the AGPL.

**New enterprise capabilities go in `ee/` from their first commit.** SSO/SAML,
an organisation-wide role directory, extended reporting — anything a company
buys because it is a company.

**Everything else goes in `src/` under AGPL**, including things that look
commercial: the audit console, usage and spend, compliance checks, installation
metrics. They stay free because taking them from a self-hosting team is a cost
to that team and no revenue to anybody.

The test: if switching a feature off would make the free build **less safe** or
**less correct**, it is not an enterprise feature. Security controls are never
enterprise-only — the audit *log* is always written, whatever a licence says
about the console for reading it.

`ee/` is empty today. The boundary exists early because adding it later means
re-asking every contributor who has already sent work under an unqualified
AGPL, and a contribution arrives under the licence it was made under.

## Getting set up

```bash
cp .env.example .env && ./scripts/init-env.sh   # generates the secrets
docker compose --env-file .env up -d
```

For work on the Python side without containers:

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
```

`docs/deploy/` holds our own deployment notes. They are scenarios that worked
for us, not instructions — read `INSTALL.md` for the supported path.

## Before you open a pull request

```bash
.venv/bin/python -m pytest tests/ -q      # all of it, not just your area
.venv/bin/python scripts/lint_ratchet.py  # ruff must not get worse
```

The ratchet fails when the finding count **grows**. New code meets the standard
immediately; the backlog is paid down by whoever is already in the file.

## What a test here has to do

Two rules, both learned the hard way and both enforced in review.

**1. Key on behaviour, not on text.** A test that greps source for a word
passes on the comment explaining that word's absence — that has happened here
more than once. If you must assert something structural, parse the AST and
assert on the node.

**2. Prove the test can fail.** Revert your fix, run the test, watch it go red,
put the fix back. A test that passes with the fix removed proves nothing, and
you will not find that out later — nobody re-checks a green test.

A corollary worth stating: **a test that breaks when the code improves was
keyed on the wrong thing.** If moving a function to a better home turns a test
red without changing any behaviour, fix the test, not the move.

## Commit messages

Say what was wrong and why it mattered, not what the diff shows. The diff is
already in the commit. Someone reading this in a year wants the reasoning.

## Reporting a security issue

Do not open a public issue. Open a private security advisory on the repository
instead.
