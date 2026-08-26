"""Tests для snapshot storage backends — Local default, S3 mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.review.storage import (
    LocalSnapshotBackend,
    _compress_file,
    _decompress_file,
    get_snapshot_backend,
)


@pytest.fixture
def tmp_root(tmp_path) -> Path:
    return tmp_path / "snap-root"


@pytest.fixture
def fake_db(tmp_path) -> Path:
    """Створити mock 'graph file'."""
    db = tmp_path / "graph.fdblite"
    # 64KB sample data — щоб compression mattered
    db.write_bytes(b"FALKORDB\x00" * 8000)
    return db


# ─── Compression ────────────────────────────────────────────────


class TestCompression:
    def test_roundtrip(self, tmp_path) -> None:
        original = tmp_path / "orig.bin"
        compressed = tmp_path / "out.gz"
        decompressed = tmp_path / "decoded.bin"
        payload = b"hello-world-" * 1000
        original.write_bytes(payload)

        size, sha = _compress_file(original, compressed)
        assert size > 0
        assert len(sha) == 64  # sha256 hex

        _decompress_file(compressed, decompressed)
        assert decompressed.read_bytes() == payload


# ─── LocalSnapshotBackend ───────────────────────────────────────


class TestLocalBackend:
    def test_upload_and_download(self, tmp_root, fake_db) -> None:
        backend = LocalSnapshotBackend(root=tmp_root)
        meta = backend.upload_snapshot(
            "github_org-repo", "abc123", fake_db,
        )
        assert meta.commit_sha == "abc123"
        assert meta.size_bytes > 0
        assert meta.sha256

        # Pointer file створений
        pointer = tmp_root / "github_org-repo" / "latest.json"
        assert pointer.exists()
        # Snapshot existsує
        snapshot = tmp_root / "github_org-repo" / "abc123" / "graph.gz"
        assert snapshot.exists()

        # Download
        target = tmp_root / "downloaded.fdblite"
        downloaded_meta = backend.download_latest("github_org-repo", target)
        assert downloaded_meta is not None
        assert downloaded_meta.commit_sha == "abc123"
        assert target.exists()
        # Content matches
        assert target.read_bytes() == fake_db.read_bytes()

    def test_download_missing_returns_none(self, tmp_root, tmp_path) -> None:
        backend = LocalSnapshotBackend(root=tmp_root)
        result = backend.download_latest("ghost-repo", tmp_path / "out")
        assert result is None

    def test_list_snapshots(self, tmp_root, fake_db) -> None:
        backend = LocalSnapshotBackend(root=tmp_root)
        backend.upload_snapshot("repo", "sha1", fake_db)
        backend.upload_snapshot("repo", "sha2", fake_db)
        backend.upload_snapshot("repo", "sha3", fake_db)

        snapshots = backend.list_snapshots("repo")
        assert len(snapshots) == 3
        shas = {s.commit_sha for s in snapshots}
        assert shas == {"sha1", "sha2", "sha3"}

    def test_list_empty_repo(self, tmp_root) -> None:
        backend = LocalSnapshotBackend(root=tmp_root)
        assert backend.list_snapshots("ghost") == []

    def test_delete_snapshot(self, tmp_root, fake_db) -> None:
        backend = LocalSnapshotBackend(root=tmp_root)
        backend.upload_snapshot("repo", "sha1", fake_db)
        assert backend.delete_snapshot("repo", "sha1") is True
        # Listing пусте після видалення
        assert backend.list_snapshots("repo") == []

    def test_delete_missing_returns_false(self, tmp_root) -> None:
        backend = LocalSnapshotBackend(root=tmp_root)
        assert backend.delete_snapshot("repo", "ghost") is False

    def test_overwrite_updates_pointer(self, tmp_root, fake_db) -> None:
        backend = LocalSnapshotBackend(root=tmp_root)
        backend.upload_snapshot("repo", "old-sha", fake_db)
        backend.upload_snapshot("repo", "new-sha", fake_db)

        pointer = tmp_root / "repo" / "latest.json"
        import json
        data = json.loads(pointer.read_text())
        assert data["commit_sha"] == "new-sha"


# ─── Factory ────────────────────────────────────────────────────


class TestFactory:
    def test_default_returns_local(self, monkeypatch) -> None:
        monkeypatch.delenv("REVIEW_S3_BUCKET", raising=False)
        from src.review.settings import get_review_settings
        get_review_settings.cache_clear()
        backend = get_snapshot_backend()
        assert isinstance(backend, LocalSnapshotBackend)

    def test_s3_falls_back_якщо_boto3_missing(self, monkeypatch) -> None:
        """Якщо REVIEW_S3_BUCKET set але boto3 не installed → fallback до Local."""
        monkeypatch.setenv("REVIEW_S3_BUCKET", "fake-bucket")
        from src.review.settings import get_review_settings
        get_review_settings.cache_clear()
        backend = get_snapshot_backend()
        # boto3 not installed → fallback
        assert isinstance(backend, LocalSnapshotBackend)
