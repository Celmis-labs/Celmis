"""Celmis — code intelligence and AI pull-request review across repositories."""

import os as _os

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
__version__ = "0.2.0"


# LiteLLM is a library here, not a proxy, and must not read a `.env` of its own.
#
# `import litellm` runs `dotenv.load_dotenv()` unless LITELLM_MODE says
# otherwise, and with no path that walks up from litellm's OWN file in
# site-packages. In a checkout whose virtualenv sits in the main tree, that
# finds the main tree's `.env` — not this worktree's, and not the one Settings
# was told to read — and copies it into `os.environ` the first time anything
# imports litellm. Every setting read after that point sees different values
# from every setting read before it, so configuration depended on import order:
# a cached `get_review_settings()` built before the first LLM call disagreed
# with a fresh `ReviewSettings()` built after it. Configuration reaches this
# process through the environment and through Settings' explicit `env_file`,
# never as a side effect of importing a dependency. `setdefault`, so an
# operator who sets LITELLM_MODE still decides.
_os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
del _os
