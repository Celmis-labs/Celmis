"""Two defaults for one setting, and the compose file wins.

`docker-compose.yml` passes most settings through as
`KEY: "${KEY:-<default>}"`. That default is a SECOND answer to a question the
Settings field already answers, and it is the one that reaches the process:
compose substitutes it before the field is ever consulted, so raising a field
default changes nothing on a deployment while the code, the tests and
`--help` all say it did.

It has happened here. `REVIEW_TIMEOUT_SECONDS` was measured against 517 real
reviews — median 74s, p90 328s — and the field was raised from 300 to 900
because a 300s deadline cut 14.3% of them. The compose line still said 300,
and the compose line won.

The comment above that line promises "розходження побачить тест
`test_a_setting_the_deployment_drops_is_not_settable`". There was no such
test. This is it, so the promise is now true rather than reassuring.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from src.config import Settings
from src.review.settings import ReviewSettings

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

#: `KEY: "${KEY:-default}"` — the same name on both sides, which is what makes
#: the compose value a default rather than a rename.
PASSTHROUGH = re.compile(r'^\s*([A-Z][A-Z0-9_]*):\s*"\$\{\1:-([^}"]*)\}"', re.M)

#: Deliberately different inside a container, with the reason. Anything not
#: named here has to match its field, so adding to this list is a decision
#: somebody makes on purpose rather than a drift nobody notices.
DELIBERATE = {
    # The field default is empty, which means the embedded local Qdrant. In
    # compose there is a Qdrant service on the network and it is the right
    # answer for that deployment, not a second opinion about the same one.
    "QDRANT_URL": "the compose network has a qdrant service; empty means embedded",
}

#: Below this the mapping has broken and the test is passing on nothing.
MINIMUM_COMPARED = 20


def _field_defaults() -> dict[str, object]:
    out: dict[str, object] = {}
    for model in (Settings, ReviewSettings):
        prefix = model.model_config.get("env_prefix") or ""
        for name, field in model.model_fields.items():
            out[(prefix + name).upper()] = field.default
    return out


#: What pydantic reads as true when the value arrives from the environment.
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _same(compose_value: str, field_default: object) -> bool:
    """Equal as the process will see them, not as strings.

    Three ways one answer gets written twice: `120` and `120.0` for a float
    field, `true` and `True` for a bool, and the plain match. Comparing the
    spellings instead of the values would fail on every boolean in the file
    and teach the next reader to ignore this test.
    """
    text = compose_value.strip()
    if isinstance(field_default, bool):
        if text.lower() in _TRUE:
            return field_default is True
        if text.lower() in _FALSE:
            return field_default is False
        return False
    if str(field_default) == compose_value:
        return True
    try:
        return float(compose_value) == float(field_default)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _pairs() -> list[tuple[str, str, object]]:
    defaults = _field_defaults()
    pairs = []
    for key, value in PASSTHROUGH.findall(COMPOSE.read_text()):
        if key not in defaults:
            continue
        default = defaults[key]
        if default is None:          # no field default to disagree with
            continue
        pairs.append((key, value, default))
    return pairs


def test_the_comparison_is_actually_comparing_something() -> None:
    compared = [p for p in _pairs() if p[0] not in DELIBERATE]
    assert len(compared) >= MINIMUM_COMPARED, (
        f"only {len(compared)} compose defaults matched a settings field. "
        f"The name mapping has broken and the test below now passes on almost "
        f"nothing."
    )


@pytest.mark.parametrize("key,compose_value,field_default", _pairs(),
                         ids=[p[0] for p in _pairs()])
def test_compose_does_not_override_the_field_default(
    key: str, compose_value: str, field_default: object,
) -> None:
    if key in DELIBERATE:
        pytest.skip(f"deliberate: {DELIBERATE[key]}")
    assert _same(compose_value, field_default), (
        f"docker-compose.yml passes {key}={compose_value!r} while the settings "
        f"field defaults to {field_default!r}. The compose value is the one "
        f"that reaches the process, so changing the field changes nothing on a "
        f"deployment. Make them agree, or add {key} to DELIBERATE with the "
        f"reason it differs."
    )


# ─── the other half: a setting the deployment never passes on ────────
#
# The file name says "a setting the deployment DROPS". Everything above is
# about compose answering a question twice; this is about compose not passing
# the question along at all. `docker-compose.yml` uses no `env_file`, so the
# api container receives exactly the variables its `environment:` block names.
# README documented `CELMIS_DEPLOYMENT_MODE` as settable and compose never
# named it: `env | grep CELMIS_DEPLOYMENT_MODE` inside the running production
# container returned nothing. A documented switch that cannot reach the process
# is worse than an undocumented one, because somebody sets it and believes it.

README = Path(__file__).resolve().parents[2] / "README.md"

#: `| `VAR` | default | what it does |` — the settings tables.
DOCUMENTED = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", re.M)


def _documented_settings() -> list[str]:
    return list(dict.fromkeys(DOCUMENTED.findall(README.read_text())))


def test_the_readme_tables_are_still_being_read() -> None:
    assert len(_documented_settings()) >= 5, (
        "almost no settings were found in the README tables; the row format "
        "changed and the test below now checks nothing"
    )


def _passed_through() -> set[str]:
    """Keys in some service's `environment:` mapping.

    Read from the parsed YAML, not by searching the text. The comment above
    the line this test was written for names the variable, so a substring
    check passed on the prose after the line itself was deleted — which is the
    exact failure mode this file exists to catch, one level up.
    """
    doc = yaml.safe_load(COMPOSE.read_text()) or {}
    keys: set[str] = set()
    for service in (doc.get("services") or {}).values():
        env = service.get("environment")
        if isinstance(env, dict):
            keys |= set(env)
        elif isinstance(env, list):
            keys |= {str(item).split("=", 1)[0] for item in env}
    return keys


def test_the_environment_blocks_are_being_read() -> None:
    assert len(_passed_through()) >= 20, (
        "almost no environment keys were parsed out of docker-compose.yml; "
        "the test below now passes on an empty set"
    )


@pytest.mark.parametrize("variable", _documented_settings())
def test_a_documented_setting_reaches_the_container(variable: str) -> None:
    assert variable in _passed_through(), (
        f"README documents {variable} as a setting, and no service's "
        f"environment block names it. There is no env_file here, so a "
        f"container is handed the variables its block lists and nothing else: "
        f"setting {variable} in .env does nothing at all."
    )
