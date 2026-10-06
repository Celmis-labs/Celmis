"""A setting .env.example documents must reach the api container.

The api service has no `env_file:` and the image carries no `.env`, so the
only route into Settings is a name in its compose `environment:` block. A
variable documented in .env.example and absent there takes its code default
whatever the operator writes — LITELLM_PROXY_ALLOWED_HOSTS did exactly that,
and the refusal it caused named the very variable the operator had set.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from src.config import Settings
from src.review.settings import ReviewSettings

ROOT = Path(__file__).resolve().parents[2]

#: Gaps that predate this test, each its own fix. The list may only shrink:
#: a name added here is a documented setting that silently does nothing.
_KNOWN_GAPS = frozenset({
    "EMBEDDING_TASK_TYPE_ENABLED",
    "GENERATION_AGENT_TIMEOUT_SECONDS",
    "GENERATION_NUM_RETRIES",
    "GENERATION_TIMEOUT_SECONDS",
})


def _api_environment() -> dict[str, str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["api"]["environment"]
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def _documented_settings() -> set[str]:
    text = (ROOT / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))
    return documented & {name.upper() for name in Settings.model_fields}


def test_every_documented_setting_is_forwarded():
    missing = _documented_settings() - set(_api_environment()) - _KNOWN_GAPS
    assert not missing, (
        "documented in .env.example, read by Settings, never forwarded to the "
        "api container:\n  " + "\n  ".join(sorted(missing))
    )


def test_the_known_gaps_are_still_gaps():
    """A gap that got fixed leaves the list, so the list stays a todo."""
    assert not (_KNOWN_GAPS & set(_api_environment()))


def test_the_proxy_allow_list_default_parses(monkeypatch):
    """`${X:-}` would hand pydantic "" for a list field and stop the API."""
    value = _api_environment()["LITELLM_PROXY_ALLOWED_HOSTS"]
    m = re.fullmatch(r"\$\{LITELLM_PROXY_ALLOWED_HOSTS:-(.*)\}", value)
    assert m, value
    monkeypatch.setenv("LITELLM_PROXY_ALLOWED_HOSTS", m.group(1))
    assert Settings().litellm_proxy_allowed_hosts == []
    monkeypatch.setenv("LITELLM_PROXY_ALLOWED_HOSTS", '["litellm.corp.internal"]')
    assert Settings().litellm_proxy_allowed_hosts == ["litellm.corp.internal"]


# ─── what the docs name, not only what .env.example lists ────────────

_NAME = re.compile(r"\b((?:CELMIS|REVIEW|JIRA|MCP|SECRET)_[A-Z0-9_]+)\b")
_QUOTED_NAME = re.compile(r"[\"']((?:CELMIS|REVIEW|JIRA|MCP)_[A-Z0-9_]+)[\"']")

#: Names the docs mention that are not operator settings of the api container
#: (a placeholder in an example, a name the web or the CLI reads, a label).
_NOT_API_SETTINGS = frozenset({
    "CELMIS_URL", "CELMIS_TOKEN", "CELMIS_E2E_STRICT", "CELMIS_REGISTRY", "CELMIS_TAG",
    "CELMIS_LICENSE_KEY", "CELMIS_LICENSE_FILE",
})


def _unreleased_changelog() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.index("## [Unreleased]")
    nxt = re.search(r"^## \[\d", text[start + 1:], re.M)
    return text[start: start + 1 + nxt.start()] if nxt else text[start:]


def _names_the_docs_give_the_operator() -> set[str]:
    texts = [p.read_text(encoding="utf-8") for p in sorted((ROOT / "docs").glob("*.md"))]
    texts += [(ROOT / "README.md").read_text(encoding="utf-8"), _unreleased_changelog()]
    return set(_NAME.findall("\n".join(texts)))


def _names_the_code_reads() -> set[str]:
    """Settings fields (as env names) and every CELMIS_/REVIEW_/JIRA_/MCP_ name
    the product reads from `os.environ` by a string literal."""
    names = {n.upper() for n in Settings.model_fields}
    names |= {"REVIEW_" + n.upper() for n in ReviewSettings.model_fields}
    tracked = subprocess.run(["git", "ls-files", "src", "ee"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    for rel in tracked:
        if rel.endswith(".py"):
            names |= set(_QUOTED_NAME.findall((ROOT / rel).read_text(encoding="utf-8")))
    return names


def test_every_setting_the_docs_name_is_forwarded_to_the_api():
    """docs/MCP_DEV.md says `CELMIS_MCP_RATE_PER_MINUTE=0` switches the limit
    off; under the shipped compose it changed nothing, because the name never
    reached the container."""
    named = _names_the_docs_give_the_operator() & _names_the_code_reads()
    missing = named - set(_api_environment()) - _KNOWN_GAPS - _NOT_API_SETTINGS
    assert not missing, (
        "named in the docs and read by the product, but never forwarded to the "
        "api container (add them to docker-compose.yml and .env.example):\n  "
        + "\n  ".join(sorted(missing))
    )


def test_the_extra_secret_globs_read_empty_a_list_or_json(monkeypatch):
    """Compose forwards `SECRET_PATH_GLOBS_EXTRA` even when the operator set
    nothing, which arrives as an empty string. That used to be a settings error
    on start-up; an empty value is none, and a comma list works next to JSON."""
    from src.config import Settings

    for raw, expected in (("", []), ("  ", []), ("a/**, b/*", ["a/**", "b/*"]),
                          ('["x/**"]', ["x/**"])):
        monkeypatch.setenv("SECRET_PATH_GLOBS_EXTRA", raw)
        assert Settings().secret_path_globs_extra == expected
