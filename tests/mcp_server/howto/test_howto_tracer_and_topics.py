"""The tracer reports names, forms and file:line for where a value comes from;
the topic matcher maps free words to one of the seven topics."""

from __future__ import annotations

import pytest

from src.mcp_server.howto import tracer
from src.mcp_server.howto.detectors import TOPICS, topic_for


def _names(*names: str) -> dict[str, tracer.NameInfo]:
    return {n: tracer.NameInfo(n, tracer.classify_name(n)) for n in names}


def _trace(tmp_path, files: dict[str, str], *names: str) -> dict[str, tracer.NameInfo]:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    found = _names(*names)
    tracer.trace_sources(found, tmp_path, list(files))
    return found


def test_a_name_listed_in_env_example_is_traced_to_that_file_without_its_value(tmp_path):
    found = _trace(tmp_path, {".env.example": "DATABASE_URL=postgres://u:pw-9f8e7d6c@h/db\n"}, "DATABASE_URL")
    assert found["DATABASE_URL"].sources == [".env.example:1"]
    assert "pw-9f8e7d6c" not in repr(found)


def test_a_compose_reference_is_traced_with_its_service_and_form(tmp_path):
    body = "services:\n  api:\n    environment:\n      DATABASE_URL: ${DATABASE_URL}\n"
    found = _trace(tmp_path, {"docker-compose.yml": body}, "DATABASE_URL")
    assert found["DATABASE_URL"].sources == ["docker-compose.yml:4 api.environment ${DATABASE_URL}"]


def test_a_kubernetes_secret_key_ref_names_the_secret_and_key_never_the_data(tmp_path):
    body = (
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n"
        "      - env:\n        - name: DATABASE_URL\n          valueFrom:\n            secretKeyRef:\n"
        "              name: db-creds\n              key: url\n"
    )
    found = _trace(tmp_path, {"deploy.yaml": body}, "DATABASE_URL")
    assert found["DATABASE_URL"].sources == ["deploy.yaml:8 k8s secretKeyRef db-creds/url"]


def test_a_bitbucket_pipeline_use_is_labelled_as_a_pipeline_variable(tmp_path):
    body = "pipelines:\n  default:\n    - step:\n        script:\n          - echo $DATABASE_URL\n"
    found = _trace(tmp_path, {"bitbucket-pipelines.yml": body}, "DATABASE_URL")
    assert any("Bitbucket" in s for s in found["DATABASE_URL"].sources)


def test_a_name_that_appears_nowhere_has_no_source(tmp_path):
    found = _trace(tmp_path, {".env.example": "OTHER=1\n"}, "DATABASE_URL")
    assert found["DATABASE_URL"].sources == []


def test_files_that_hold_secrets_are_not_used_as_a_source(tmp_path):
    found = _trace(tmp_path, {".env": "DATABASE_URL=postgres://u:pw-1a2b3c4d@h/db\n"}, "DATABASE_URL")
    assert found["DATABASE_URL"].sources == []


def test_a_traced_file_that_is_really_a_kubernetes_secret_is_skipped(tmp_path):
    body = "apiVersion: v1\nkind: Secret\ndata:\n  DATABASE_URL: cGctOWY4ZTdkNmM=\n"
    found = _trace(tmp_path, {"k8s/db.yaml": body}, "DATABASE_URL")
    assert found["DATABASE_URL"].sources == []


@pytest.mark.parametrize(
    ("snippet", "name", "has_default"),
    [
        ('x = os.getenv("A_B", "d")', "A_B", True),
        ('x = os.environ["C_D"]', "C_D", False),
        ('const x = process.env.E_F || "d"', "E_F", None),
        ('String x = System.getenv("G_H");', "G_H", False),
        ('v := os.Getenv("I_J")', "I_J", False),
        ("$x = getenv('K_L');", "K_L", False),
        ('var x = Environment.GetEnvironmentVariable("M_N");', "M_N", False),
    ],
)
def test_env_reads_are_found_in_each_language(snippet, name, has_default):
    found = tracer.extract_names(snippet, "src/x", 10)
    assert name in found
    assert found[name].reads == ["src/x:10"]
    if has_default is not None:
        assert found[name].default_in_code is has_default


def test_a_read_reports_the_real_line_in_a_slice_that_starts_later():
    found = tracer.extract_names('a = 1\nb = os.environ["C_D"]\n', "a.py", 40)
    assert found["C_D"].reads == ["a.py:41"]


def test_settings_fields_get_their_prefixed_and_aliased_env_names():
    text = (
        "from pydantic_settings import BaseSettings\n"
        "class S(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="APP_")\n'
        '    db_url: str = "x"\n'
        '    tok: str = Field(validation_alias="MY_TOK")\n'
    )
    fields = {f.field: f for f in tracer.parse_settings_classes(text, "c.py")}
    assert fields["db_url"].env == "APP_DB_URL"
    assert fields["tok"].env == "MY_TOK"
    assert fields["db_url"].loc == "c.py:4"


def test_a_settings_default_value_is_never_part_of_the_result():
    text = 'class S(BaseSettings):\n    db_password: str = "pw-5e4d3c2b1a"\n'
    assert "pw-5e4d3c2b1a" not in repr(tracer.parse_settings_classes(text, "c.py"))


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("DATABASE_URL", "secret"),
        ("API_TOKEN", "secret"),
        ("JWT_SECRET", "secret"),
        ("MAX_TOKENS", "config"),
        ("DB_HOST", "config"),
        ("TOKEN_FILE", "config"),
        ("LOG_LEVEL", "config"),
    ],
)
def test_names_are_classified_as_secret_or_plain_config(name, kind):
    assert tracer.classify_name(name) == kind


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("db", "db"),
        ("database connection", "db"),
        ("jwt login", "auth"),
        ("redis ttl", "cache"),
        ("kafka queue", "messaging"),
        ("logger", "logging"),
        ("requests timeout", "http_client"),
        ("settings", "config"),
    ],
)
def test_free_words_map_to_a_topic(text, topic):
    assert topic_for(text) == topic


def test_words_that_match_nothing_map_to_no_topic():
    assert topic_for("zzz") is None and topic_for("") is None


def test_every_topic_name_maps_to_itself():
    assert all(topic_for(t) == t for t in TOPICS)
