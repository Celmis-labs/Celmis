# Security policy

## Reporting a vulnerability

Write to **kostiantynmakoid@gmail.com** with *security* in the subject line.

Please include:

- what you did, and what happened;
- the version you were running — the footer of any running instance carries the exact
  commit, in the form `0.1.9+abc1234`;
- whether the issue is reachable by an unauthenticated caller, and from where.

You will get an acknowledgement within three working days. When a fix is available, the
advisory and the fix are published together. If you would like to be credited, say so and
you will be.

**Please do not** open a public issue for an unpatched vulnerability, and please do not
test against anyone else's running instance.

## Supported versions

Fixes land on the latest release. There is no long-term support branch: this is a small
project, and pretending otherwise would be a promise it cannot keep.

## What this project is not

Celmis produces an evidence pack — a CycloneDX SBOM, findings, a timeline and a manifest
of sha256 digests over all of them — that a third party can verify without trusting the
machine that produced it.

It does **not** claim that using it makes your product compliant with the EU Cyber
Resilience Act or any other regulation, and it says so in its own output. Compliance is a
judgement about an organisation, not an artefact a program can emit.
