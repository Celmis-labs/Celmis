"""Tests для ReviewSettings env loading + defaults."""

from __future__ import annotations

from src.review.settings import ReviewSettings


class TestDefaults:
    def test_default_values(self) -> None:
        s = ReviewSettings()
        assert s.hot_cache_size == 8
        assert s.hot_ttl_seconds == 600
        # The MODEL NAMES are not asserted here any more, and the reason is
        # that this assertion defended a bug for as long as it existed.
        #
        # It read `s.architect_model == "gemini-3-pro"`. That model does not
        # exist — Google's list has no such entry and litellm answers
        # NotFoundError — so the architect, security and verifier agents
        # failed TERMINAL on every review of a default install. Correcting the
        # default broke this test, which is backwards: a test that fails when
        # the code is fixed was pinning the defect, not the behaviour.
        #
        # What a model default has to BE is asserted in
        # tests/review/test_every_default_model_is_a_model_that_exists.py —
        # it must resolve — which is true of whatever name is correct next
        # year as well.
        assert s.defect_model
        assert s.contract_model
        assert s.max_inline_comments == 20
        assert s.replace_on_synchronize is True

    def test_has_s3_false_by_default(self) -> None:
        assert ReviewSettings().has_s3 is False

    def test_has_redis_false_by_default(self) -> None:
        assert ReviewSettings().has_redis is False


    def test_agent_concurrency_defaults_to_half_the_roster(self) -> None:
        # Six agents used to mean six simultaneous provider connections —
        # the measured source of ConnectError on a weak uplink.
        assert ReviewSettings().agent_concurrency == 3


class TestEnvOverride:
    def test_env_prefix(self, monkeypatch) -> None:
        monkeypatch.setenv("REVIEW_HOT_CACHE_SIZE", "16")
        monkeypatch.setenv("REVIEW_S3_BUCKET", "my-bucket")
        s = ReviewSettings()
        assert s.hot_cache_size == 16
        assert s.s3_bucket == "my-bucket"
        assert s.has_s3 is True

    def test_agent_concurrency_reads_the_env(self, monkeypatch) -> None:
        monkeypatch.setenv("REVIEW_AGENT_CONCURRENCY", "5")
        assert ReviewSettings().agent_concurrency == 5

    def test_redis_url_enables_has_redis(self, monkeypatch) -> None:
        monkeypatch.setenv("REVIEW_REDIS_URL", "redis://localhost:6379/0")
        s = ReviewSettings()
        assert s.has_redis is True


class TestSkipPatterns:
    def test_default_skip_patterns_present(self) -> None:
        s = ReviewSettings()
        # Common patterns
        assert any("lock" in p for p in s.skip_filename_patterns)
        assert "node_modules" in s.skip_directory_patterns
        assert "vendor" in s.skip_directory_patterns
