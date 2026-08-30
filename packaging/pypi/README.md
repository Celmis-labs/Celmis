# celmis

**This package is the offline verifier for a Celmis evidence pack. It is not the
Celmis platform.**

The platform is six services under docker compose, and no sensible person would
install that with pip. This package is one command:

    pip install celmis
    celmis verify evidence-pack.zip

It takes an evidence-pack archive, recomputes the sha256 of every file listed in
the manifest inside it, and tells you whether the pack is the one that was
generated. It has no dependencies, makes no network calls, and never contacts a
Celmis installation.

That isolation is the whole design. The person who has to check a pack is
usually not the operator who produced it — an auditor, a customer's security
reviewer, somebody holding a zip that arrived by email. Asking them to install
the producer's tooling in order to check the producer's output defeats the
purpose of checking. So the checker is a separate, dependency-free program that
reads a file.

Installing the platform is a git clone and a docker compose up, described at the
bottom of this page.

## What the verifier checks

A Celmis evidence pack is a zip containing:

    sbom/<repo>.cdx.json   one CycloneDX 1.5 inventory per repository
    findings.json          every vulnerability, with severity and fixed version
    timeline.jsonl         when each audit run happened and what it found
    summary.md             the human-readable cover sheet
    MANIFEST.json          a sha256 for every file above

`celmis verify` reports three kinds of problem and distinguishes between them:

- a file listed in the manifest that is absent from the archive,
- a file whose recomputed sha256 does not match the manifest,
- a file present in the archive that the manifest does not vouch for.

The third matters as much as the second. An added file is content nobody signed.

Packs are built deterministically — fixed archive timestamps, sorted entries,
sorted-key JSON — so two exports of the same audit run produce identical bytes.
That is what makes "this pack was not regenerated with different contents" a
statement somebody can test rather than a statement somebody makes.

The manifest declares its own format version. A pack produced by a newer Celmis
than this verifier is reported as exactly that, and not as a failed check — "I
do not understand this" and "this has been altered" are opposite answers, and
the second is an accusation.

## Usage

    celmis verify pack.zip            # exit 0 if intact, 1 if not, 2 on a usage error
    celmis verify --json pack.zip     # machine-readable, for a CI step
    celmis show pack.zip              # print the manifest
    celmis show --summary pack.zip    # print summary.md from inside the pack
    celmis --version

A pack is exported from a running installation at
`GET /api/deps/{run_id}/evidence`.

A pack is a record of what an audit run found and when. It is not a compliance
certificate, and this tool makes no claim about whether any particular
obligation is satisfied. That is a lawyer's judgement, and a tool that implied
otherwise would be selling exactly the false confidence the subsystem exists to
avoid.

## What Celmis is

Celmis covers most of a development cycle plus the observability that feeds it,
on one machine you own. Four capabilities, in the order they matter.

### 1. See it, fix it, without leaving the platform

An alert — Grafana, or anything that can POST JSON — reaches a per-workspace
ingest URL. The alert already knows which repository owns it, and it goes
straight out to whatever channel the workspace has bound: Slack, Discord,
Google Chat or a plain webhook. *Fix from here* opens an embedded Claude Code
session that already holds the repository, the package and both versions; the
session edits a checkout and the runner pushes a branch and opens the pull
request.

Measured once at 220 seconds, on a real `lodash 4.17.11` advisory, from starting
the session to an open pull request. No laptop, no local checkout, no terminal —
the work happens inside the operator's own installation.

### 2. Vulnerabilities, SBOM and the evidence pack

Deterministic checks that read files. No model decides that something is wrong,
so the false-positive rate is zero by construction. OSV-Scanner and each
ecosystem's own auditor are treated as inputs, not as features.

The outputs are a CycloneDX SBOM per repository and the evidence pack this
package verifies — designed around the question an auditor asks about a moment
in the past: on the day this was exploited, what did you know, when did you
learn it, and what did you do. A dashboard shows the present. A record can
answer that.

### 3. Code intelligence

One symbol graph across repositories, built deterministically with tree-sitter.
No model participates in constructing it. Questions are answered across
repository boundaries with file:line citations. 23 languages plus infrastructure
formats. An MCP server exposes 18 tools over the same index under the same
access rules. A sentence box acts on a *set* of repositories at once, with a
second press required and the scope re-checked at confirmation.

### 4. Pull-request review

A good addition with a decent score, and not the centre of the product.

On the Martian Code Review Bench offline set, the run placed 17th of 50 under
all three judges. Precision was 48.0% under the `claude-sonnet-4-5-20250929`
judge — the middle of the three rather than the most favourable, and the one
whose false positives were then audited; the other two scored the same run at
52.4% and 46.0%. The run cost $5.88 in total, $0.118 per pull request.

All 79 findings that judge scored as false positives were opened in the source
at the reviewed commit and given a verdict, each published with its code and a
pinned permalink: 33 were real defects the gold set does not contain, 38 were
genuinely wrong, 8 were unverifiable. The article reporting that also records
that the cross-repository capability was structurally unavailable on that set —
0 of the 50 pull requests carried a drift finding, 0 had a cross-repository
caller, and graph context was rejected as `base_too_old` on 18 of them. The
corrected precision computed in that article **cannot be compared with any other
row on the board**, because only these false positives were audited and nobody
else's; 48.0% is the figure that is comparable, and it is the one that stays.

The benchmark measures item 4 only. It is not a measurement of items 1 to 3.

## Installing the platform

    git clone https://github.com/Celmis-labs/Celmis
    cd Celmis
    cp .env.example .env
    docker compose up -d

197 seconds from `git clone` to six healthy services, measured on a clean
server. Postgres and Qdrant are bundled. About 4 GB of free RAM is the
recommendation; a real indexing run measured 1.1 GB peak across the containers
and 565 MB at rest.

The model provider is the operator's choice: Gemini, Anthropic, OpenAI,
OpenRouter, Groq, Mistral, or a local server — Ollama, vLLM, llama.cpp — for
generation *and* embeddings, which is what makes an air-gapped installation
possible.

## Controls

- Per-repository access control, where deny-globs win over allow.
- GDPR export and erasure.
- A monthly spend cap that actually refuses calls rather than warning about them.
- An append-only JSONL audit trail, with CSV export.

## Licence

AGPL-3.0-or-later, with one exception drawn by path. `LICENSE` is the
unmodified AGPL text; the exception and the reasoning behind it are in
`LICENSING.md`, and the terms for the excepted files are in `LICENSE_EE`.
Anything whose path contains `ee/`, and any file whose name contains `.ee.`, is
governed by `LICENSE_EE` rather than the AGPL. That directory holds no product
code today. The boundary was drawn before it was needed, because adding it after
the first outside contribution means re-asking every contributor who already
sent work under an unqualified AGPL.

The exception lives in `LICENSING.md` rather than at the top of `LICENSE`
because a licence file with a preamble in front of it stops being recognised as
that licence — GitHub compares the file against a reference text and answers
`NOASSERTION`, which reads to a package index as no licence at all.

Everything shipped at the first public release is AGPL, including the parts that
look commercial — the audit console, usage and spend, compliance checks,
installation metrics. Security controls are never enterprise-only.

## Who maintains this

One person. A solo maintainer, still a student, graduating in 2027.

## Links

- Site: https://celmis-labs.github.io
- Source: https://github.com/Celmis-labs/Celmis
- MCP server: https://celmis-labs.github.io/mcp/
- The false-positive audit: https://celmis-labs.github.io/writing/auditing-79-false-positives/
