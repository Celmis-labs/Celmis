# Published digests for `celmis`

The sha256 of every file published to PyPI, recorded here — in the git
repository, on GitHub — because PyPI is the same channel that serves the files.
A hash published only beside what it describes proves nothing, which is the
whole argument the verifier itself makes about an evidence pack's manifest.

## What this record is, and what it is not

**It is a second channel.** GitHub and PyPI are different parties with
different credentials. A file substituted on one of them stops matching the
other, and anybody can check that with one command.

**It is not independent attestation for what is already published.** The
digests for 0.1.0 and 0.2.0 below were *read from PyPI* on 2026-08-31 and
written down. If those files had already been substituted before that date,
this file faithfully records the substitution. It is a trust-on-first-use
anchor, and saying otherwise would be the same over-claim this project keeps
correcting in itself.

From the first release published by `.github/workflows/publish-verifier.yml`
the position improves twice over: the digests are printed in the workflow log
before upload, by a runner nobody had to trust afterwards, and the artefacts
carry a PyPI attestation minted from the workflow's own identity. Those two
are evidence. This file is a ledger.

## Verifying

    pip download --no-deps celmis==0.2.0
    sha256sum celmis-0.2.0-py3-none-any.whl

or make pip refuse anything else:

    # requirements.txt
    celmis==0.2.0 \
      --hash=sha256:82130b002ff45e8db4e9098c91578b447b2aa2cfee5051abb9c8ff982f4d22c1 \
      --hash=sha256:010b3233756d1acc18cc6fcd09c11dbf393ddcc32f5928b4442df029405e41f3

    pip install --require-hashes -r requirements.txt

`--require-hashes` needs a hash for every distribution it may choose, which is
why both the wheel and the sdist are listed.

## 0.2.0 — 2026-08-30

| file | sha256 |
|---|---|
| `celmis-0.2.0-py3-none-any.whl` | `82130b002ff45e8db4e9098c91578b447b2aa2cfee5051abb9c8ff982f4d22c1` |
| `celmis-0.2.0.tar.gz` | `010b3233756d1acc18cc6fcd09c11dbf393ddcc32f5928b4442df029405e41f3` |

Read from PyPI 2026-08-31. Published by hand; `provenance` is `None`.

## 0.1.0 — 2026-08-30

| file | sha256 |
|---|---|
| `celmis-0.1.0-py3-none-any.whl` | `c205243bc8e411838a3dd6e196b73d1b87f2765f0feab163bdc1f9a4728c64a1` |
| `celmis-0.1.0.tar.gz` | `ae2696c4322cc46470ca03e9c023331892326a77a3fcbe02fc4d37428b7b69d1` |

Read from PyPI 2026-08-31. Published by hand; `provenance` is `None`.

0.1.0 verified an evidence pack against its own manifest and nothing else; if
you are checking a pack rather than reading history, use 0.2.0 or later, which
takes `--manifest-sha256`.
