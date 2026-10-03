"""A setting .env.example documents must reach the api container.

The api service has no `env_file:` and the image carries no `.env`, so the
only route into Settings is a name in its compose `environment:` block. A
variable documented in .env.example and absent there takes its code default
whatever the operator writes — LITELLM_PROXY_ALLOWED_HOSTS did exactly that,
and the refusal it caused named the very variable the operator had set.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from src.config import Settings

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
