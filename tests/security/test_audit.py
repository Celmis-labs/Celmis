"""Tests для audit logger — Phase 6.

Перевіряє append-only JSONL формат, track() context manager,
що raw payloads/secrets ніколи не потрапляють у log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.security.audit import AuditLogger, AuditRecord


@pytest.fixture
def logger(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "audit.jsonl")


# ─── basic write ────────────────────────────────────────────────────


def test_write_one_record_is_jsonl(logger: AuditLogger) -> None:
    record = AuditRecord(
        request_id="abc-123",
        timestamp="2026-04-27T10:00:00Z",
        mode="qa",
        model="gemini-3.1-pro-preview",
        operation="ask",
    )
    logger.write(record)

    lines = logger.log_path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["request_id"] == "abc-123"
    assert parsed["mode"] == "qa"
    assert parsed["model"] == "gemini-3.1-pro-preview"


def test_write_multiple_records_appends(logger: AuditLogger) -> None:
    for i in range(5):
        logger.write(AuditRecord(
            request_id=f"req-{i}",
            timestamp="2026-04-27T10:00:00Z",
            mode="batch",
            model="gemini-3.1-pro-preview",
            operation="generate",
        ))
    lines = logger.log_path.read_text().splitlines()
    assert len(lines) == 5
    ids = [json.loads(line)["request_id"] for line in lines]
    assert ids == [f"req-{i}" for i in range(5)]


# ─── track() context manager ────────────────────────────────────────


def test_track_writes_on_success(logger: AuditLogger) -> None:
    with logger.track(
        mode="qa",
        model="gemini-3.1-pro-preview",
        operation="ask",
        question="Як працює логін?",
    ) as record:
        record.response_hash = AuditLogger.hash_response("answer-text")
        record.input_tokens_estimated = 1000
        record.output_tokens_estimated = 200

    line = logger.log_path.read_text().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["operation"] == "ask"
    assert parsed["question_hash"] is not None
    assert parsed["question_hash"] != "Як працює логін?"  # хеш, не plaintext
    assert parsed["response_hash"] is not None
    assert parsed["input_tokens_estimated"] == 1000
    assert parsed["duration_ms"] >= 0


def test_track_writes_on_exception(logger: AuditLogger) -> None:
    """track() МУСИТЬ записати record навіть якщо blok падає (для forensics)."""
    with pytest.raises(ValueError, match="boom"), logger.track(
        mode="batch",
        model="gemini-3.1-pro-preview",
        operation="generate",
    ):
        raise ValueError("boom")

    line = logger.log_path.read_text().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["error"] is not None
    assert "ValueError" in parsed["error"]
    assert "boom" in parsed["error"]


def test_track_question_is_hashed_not_logged_raw(logger: AuditLogger) -> None:
    """Питання логується тільки як SHA256 (truncated). Raw text — НІКОЛИ."""
    secret_in_question = "Як я можу сховати password=hunter2 у коді?"
    with logger.track(
        mode="qa",
        model="m",
        operation="ask",
        question=secret_in_question,
    ):
        pass

    log = logger.log_path.read_text()
    assert "hunter2" not in log
    assert "password=hunter2" not in log
    parsed = json.loads(log.splitlines()[0])
    assert isinstance(parsed["question_hash"], str)
    assert len(parsed["question_hash"]) == 16  # truncated to 16 hex chars


def test_track_response_hash_helper() -> None:
    """hash_response — детермінований."""
    h1 = AuditLogger.hash_response("hello world")
    h2 = AuditLogger.hash_response("hello world")
    h3 = AuditLogger.hash_response("hello world!")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16


# ─── critical: raw payload NEVER logged ─────────────────────────────


def test_secrets_in_extra_field_should_not_be_pre_filtered(logger: AuditLogger) -> None:
    """Audit покладається на caller'а — НЕ передавати raw secrets у extra.

    Цей тест — guard rail / документація: якщо хтось випадково передасть
    secret у `extra`, audit все одно його запише. Тому caller повинен
    redact'ити перед.
    """
    with logger.track(
        mode="qa",
        model="m",
        operation="ask",
        extra={"user_id": "u123"},  # OK
    ):
        pass

    line = logger.log_path.read_text().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["extra"]["user_id"] == "u123"


def test_redaction_stats_in_record(logger: AuditLogger) -> None:
    """Audit record має поле redaction для статистики від Redactor."""
    with logger.track(mode="batch", model="m", operation="generate") as record:
        record.redaction = {
            "secrets_found": 3,
            "patterns_matched": ["aws-access-key", "openai-key"],
            "bytes_in": 1500,
            "bytes_out": 1500,
        }

    parsed = json.loads(logger.log_path.read_text().splitlines()[0])
    assert parsed["redaction"]["secrets_found"] == 3
    assert "aws-access-key" in parsed["redaction"]["patterns_matched"]


# ─── append-only behavior ────────────────────────────────────────────


def test_log_is_append_only(logger: AuditLogger, tmp_path: Path) -> None:
    """Reopen logger to same path — old entries preserved."""
    logger.write(AuditRecord(
        request_id="r1", timestamp="t", mode="m", model="m", operation="o",
    ))

    # New logger instance, same path
    logger2 = AuditLogger(tmp_path / "audit.jsonl")
    logger2.write(AuditRecord(
        request_id="r2", timestamp="t", mode="m", model="m", operation="o",
    ))

    lines = logger.log_path.read_text().splitlines()
    assert len(lines) == 2
    ids = [json.loads(line)["request_id"] for line in lines]
    assert ids == ["r1", "r2"]


# ─── rotation (Phase 12) ────────────────────────────────────────────


def test_rotation_when_size_exceeds_threshold(tmp_path: Path) -> None:
    """Файл > max_size → rename до .1, новий empty file."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, max_size_bytes=500, max_files=3)

    # Write enough records to exceed 500 bytes
    for i in range(20):
        logger.write(AuditRecord(
            request_id=f"r{i}", timestamp="t", mode="m",
            model="some-very-long-model-name-3.1-pro-preview",
            operation="some-long-operation-name",
        ))

    # Має існувати .1 (rotated) + поточний файл
    rotated = log_path.with_name("audit.jsonl.1")
    assert rotated.exists(), "Expected rotated file"
    # Поточний існує (запис після rotation)
    assert log_path.exists()


def test_rotation_keeps_max_files(tmp_path: Path) -> None:
    """Rotation cap'ається на max_files — найстарші видаляються."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, max_size_bytes=200, max_files=2)

    # 5 rotations — повинно лишитись поточний + 2 (max_files)
    for batch in range(5):
        for i in range(20):
            logger.write(AuditRecord(
                request_id=f"r{batch}_{i}", timestamp="t", mode="m",
                model="model_x", operation="op_long_name_xxx",
            ))

    # Файлів аудиту: основний + .1 + .2 = 3 максимум.
    files = sorted(p.name for p in tmp_path.glob("audit.jsonl*"))
    assert len(files) <= 3, f"too many files: {files}"


def test_rotation_disabled_below_threshold(tmp_path: Path) -> None:
    """Маленький лог — rotation НЕ спрацьовує."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path, max_size_bytes=10_000_000, max_files=5)
    logger.write(AuditRecord(request_id="r1", timestamp="t", mode="m", model="x", operation="o"))
    rotated = log_path.with_name("audit.jsonl.1")
    assert not rotated.exists()


# ─── DoD: complete audit format ─────────────────────────────────────


def test_dod_audit_record_has_all_required_fields(logger: AuditLogger) -> None:
    """Plan §9.5 specifies which fields must be in audit log."""
    with logger.track(
        mode="qa",
        model="gemini-3.1-pro-preview",
        operation="ask",
        repo="acme-frontend",
        module="auth",
        question="Як працює логін?",
        files_sent=["src/auth/useAuth.ts", "src/auth/Login.vue"],
    ) as record:
        record.input_tokens_estimated = 8421
        record.output_tokens_estimated = 1024
        record.response_hash = AuditLogger.hash_response("...")
        record.redaction = {"secrets_found": 0, "patterns_matched": []}

    parsed = json.loads(logger.log_path.read_text().splitlines()[0])
    required = {
        "request_id", "timestamp", "mode", "model", "operation",
        "repo", "module", "question_hash", "files_sent",
        "input_tokens_estimated", "output_tokens_estimated",
        "redaction", "response_hash", "duration_ms",
    }
    missing = required - set(parsed.keys())
    assert not missing, f"Missing audit fields: {missing}"

    # No raw question text leaks
    assert "логін" not in str(parsed)
    # Files sent recorded
    assert "src/auth/useAuth.ts" in parsed["files_sent"]
