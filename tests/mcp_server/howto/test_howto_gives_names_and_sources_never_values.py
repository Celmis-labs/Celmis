"""``howto`` over a repository that has every kind of secret committed.

The fixture plants canaries in a ``.env``, private keys, a Kubernetes Secret,
compose, CI, hard-coded DSNs, ``appsettings.json``, ``application.properties``
and code defaults. The answer must name what an agent needs (env vars, where
their values come from) and contain none of the values, in any encoding.
"""

from __future__ import annotations

import re

import pytest

from src.mcp_server.howto import run_howto

IDX_LINE_RE = r"^idx: (?P<entries>.+)$"
IDX_ENTRY_RE = (
    r"(?P<slug>[A-Za-z0-9._/-]+) (?P<branch>\S+)@(?P<sha>[0-9a-f]{7,40}) "
    r"(?P<age>\S+) (?P<state>fresh|STALE|unknown)"
)
HIT_RE = r"^(?P<slug>\S+) (?P<path>[^\s:]+):(?P<start>\d+)(?:-(?P<end>\d+))?\b"
MORE_RE = r"^… \+(?P<n>\d+) more · cursor=(?P<cursor>\S+)"


def _ask(topic, **kw):
    return run_howto(topic, "acme/shop", **kw)


def _assert_no_canary(leaky, text):
    for c in leaky.all_canaries():
        assert c not in text, f"leaked a canary: {c[:4]}…"


@pytest.mark.parametrize("topic", ["db", "auth", "config", "http_client", "logging", "messaging", "cache"])
def test_no_planted_secret_appears_for_any_topic(leaky, topic):
    out = _ask(topic, detail="detailed", budget_tokens=6000)
    _assert_no_canary(leaky, out)


def test_the_db_answer_names_the_variables_and_where_each_value_comes_from(leaky):
    out = _ask("db")
    assert "DATABASE_URL" in out and "DB_POOL_SIZE" in out
    assert ".env.example:1" in out
    assert "docker-compose.yml" in out and "api.environment ${DATABASE_URL}" in out
    assert "k8s secretKeyRef shop-db/url" in out
    assert "Bitbucket" in out  # the pipeline variable
    assert re.search(r"DATABASE_URL.*\bsecret\b", out)
    assert re.search(r"DB_POOL_SIZE.*\bconfig\b", out)
    assert "settings.database_url" in out  # where the code gets it from
    assert "create_async_engine" in out


def test_the_answer_shows_the_pattern_code_with_secrets_replaced_by_placeholders(leaky):
    out = _ask("db", budget_tokens=6000)
    assert "postgresql+asyncpg://app:[REDACTED:dsn-password]@db" in out
    assert 'os.getenv("DB_PASSWORD", "[REDACTED:env-default]")' in out


def test_a_hard_coded_credential_is_reported_by_place_and_label_never_by_value(leaky):
    out = _ask("db", budget_tokens=6000)
    assert re.search(r"warning: hardcoded credential src/db/legacy\.py:\d+ \(dsn-password\), do not copy", out)
    assert re.search(r"warning: hardcoded credential web/db\.ts:\d+ \(dsn-password\), do not copy", out)


def test_the_answer_tells_the_agent_to_ask_for_values_and_not_to_hunt_for_them(leaky):
    out = _ask("db")
    assert "ask the user or ops" in out and "do not search for values" in out
    assert "values are never available via Celmis" in out


def test_the_auth_answer_has_the_middleware_slice_and_the_secret_names(leaky):
    out = _ask("auth")
    assert "HTTPBearer" in out and "jwt.decode" in out
    assert re.search(r"JWT_SECRET.*secret", out) and ".env.example" in out
    assert re.search(r"hardcoded credential app/auth\.py:\d+ \(auth-header\)", out)


def test_the_config_answer_lists_settings_fields_as_env_names(leaky):
    out = _ask("config")
    for name in ("DATABASE_URL", "DB_POOL_SIZE", "JWT_SECRET", "LEGACY_TOKEN"):
        assert name in out
    assert re.search(r"LEGACY_TOKEN.*default-in-code: yes", out)
    assert re.search(r"JWT_SECRET.*default-in-code: no", out)


def test_secret_files_are_not_mentioned_not_read_and_not_traced(leaky):
    for topic in ("db", "auth", "config"):
        out = _ask(topic, detail="detailed", budget_tokens=6000)
        for forbidden in ("server.key", "id_rsa", "k8s/secret.yaml", "BEGIN PRIVATE", "BEGIN OPENSSH", "shop-db\n  password"):
            assert forbidden not in out, (topic, forbidden)
        assert not re.search(r"(?<![.\w])\.env(?!\.example)\b", out), topic


def test_the_example_env_file_is_a_source_of_names_with_its_values_withheld(leaky):
    out = _ask("config")
    assert ".env.example:4" in out  # LEGACY_TOKEN is listed...
    assert leaky.canaries["settings_default"] not in out  # ...but its value is not


def test_the_first_line_is_the_freshness_line_and_the_second_names_the_query(leaky):
    lines = _ask("db").splitlines()
    m = re.match(IDX_LINE_RE, lines[0])
    assert m and re.fullmatch(IDX_ENTRY_RE, m.group("entries"))
    assert lines[1].startswith("howto db acme-shop")


def test_every_slice_header_matches_the_hit_format(leaky):
    out = _ask("db", detail="detailed", budget_tokens=6000)
    heads = [ln for ln in out.splitlines() if re.match(r"^acme-shop \S+:\d+", ln)]
    assert len(heads) >= 3
    assert all(re.match(HIT_RE, h) for h in heads)


def test_pagination_returns_the_next_slices_with_a_cursor(leaky):
    first = _ask("db", detail="concise")
    more = [ln for ln in first.splitlines() if re.match(MORE_RE, ln)]
    assert more, first
    cursor = re.match(MORE_RE, more[0]).group("cursor")
    second = _ask("db", detail="concise", cursor=cursor)
    heads1 = {ln for ln in first.splitlines() if re.match(HIT_RE, ln)}
    heads2 = {ln for ln in second.splitlines() if re.match(HIT_RE, ln)}
    assert heads2 and not heads1 & heads2


def test_detailed_gives_more_slices_than_concise(leaky):
    def heads(out):
        return [ln for ln in out.splitlines() if re.match(HIT_RE, ln)]

    assert len(heads(_ask("db", detail="detailed", budget_tokens=6000))) > len(heads(_ask("db")))


def test_the_token_budget_limits_the_slices(leaky):
    def heads(out):
        return [ln for ln in out.splitlines() if re.match(HIT_RE, ln)]

    small = _ask("db", detail="detailed", budget_tokens=60)
    big = _ask("db", detail="detailed", budget_tokens=6000)
    assert 1 <= len(heads(small)) < len(heads(big))
    assert "… +" in small  # what did not fit is counted, not dropped silently


def test_the_path_argument_limits_where_it_looks(leaky):
    out = _ask("db", path="web/")
    assert "web/db.ts" in out and "src/db/session.py" not in out


def test_tests_are_demoted_below_the_real_pattern(leaky):
    out = _ask("db", detail="detailed", budget_tokens=6000)
    heads = [ln for ln in out.splitlines() if re.match(HIT_RE, ln)]
    paths = [re.match(HIT_RE, h).group("path") for h in heads]
    if "tests/test_db.py" in paths:
        assert paths.index("tests/test_db.py") > paths.index("src/db/session.py")


def test_free_text_topics_map_to_a_topic(leaky):
    assert _ask("database connection").splitlines()[1].startswith("howto db ")
    assert _ask("how authorization works").splitlines()[1].startswith("howto auth ")
    assert _ask("credentials loading").splitlines()[1].startswith("howto config ")


def test_an_unknown_topic_lists_the_known_ones(leaky):
    out = _ask("quantum")
    assert out.startswith("idx: none") and "db" in out and "messaging" in out


def test_an_empty_result_says_so_and_still_starts_with_the_freshness_line(leaky):
    out = _ask("messaging")
    assert out.splitlines()[0].startswith("idx: ")
    assert "no messaging pattern found" in out


def test_the_whole_answer_is_redacted_again_before_it_leaves(leaky, monkeypatch):
    """Even a line that reaches the answer by a path the slice redaction does
    not cover (dependency lines in the detailed extras) is redacted."""
    from src.mcp_server.howto import engine

    monkeypatch.setattr(engine, "dependency_lines", lambda *a, **k: [f"x:1 postgresql://u:{leaky.canaries['dsn']}@h/d"])
    out = _ask("db", detail="detailed")
    assert leaky.canaries["dsn"] not in out
