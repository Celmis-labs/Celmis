"""Celmis — code intelligence and AI pull-request review across repositories."""

#: The distribution names an installed build may carry, newest first.
#:
#: The project was renamed from "code-analysis-system" to "celmis" before the
#: first tag, because after the first tag the name is in image paths, in other
#: people's compose files and in their documentation. Both are listed because
#: a container built before the rename still reports the old one, and a
#: version lookup that returns "unknown" during a rollout is a worse answer
#: than a slightly longer list.
#:
#: NOTE the inverted comment in src/vault/provenance.py: looking up "celmis"
#: used to be the BUG, back when the distribution was called something else.
#: It is now the answer. A constant that changed meaning is worth saying out
#: loud rather than leaving for the next reader to trip over.
DISTRIBUTIONS = ("celmis-platform", "celmis", "code-analysis-system")

#: The single source of truth for the version, and a LITERAL on purpose.
#:
#: `pyproject.toml` reads this attribute (`version = {attr = "src.__version__"}`)
#: and its comment explains why this file wins: it can be read without the
#: package being installed, in a test, in a source checkout and in the image.
#:
#: It stopped being a literal and became `importlib.metadata.version(...)` —
#: which closed a loop. setuptools evaluates this attribute AT BUILD TIME, when
#: the distribution is not yet installed, so it read "0.0.0+unknown", baked
#: that into the metadata, and every runtime lookup read it straight back out.
#: A fixed point at "unknown": `/api/capabilities` on production answered
#:
#:     "api_version": "0.0.0+unknown"
#:
#: and would have answered that for every release forever. A version that is
#: always the same string is worse than no version — it looks like an answer.
#:
#: 0.1.0 is what the four duplicated copies said before they were collapsed
#: into this one, and what `web/package.json` still says.
__version__ = "0.1.28"
