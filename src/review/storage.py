"""Snapshot storage backends — Local (default) + optional S3.

Stage 17.3 (May 2026).

Architecture: SnapshotBackend protocol with 2 implementations:
    LocalSnapshotBackend — file system, gzip compression (stdlib, no extra deps)
    S3SnapshotBackend    — boto3 multipart upload (optional, gracefully missing)

Snapshot keys: `{provider}/{owner}/{repo}/{commit_sha}/graph.{ext}` +
versioned `latest.json` pointer for atomicity.

Both backends share the atomic-pointer pattern:
    1. Upload `commit_sha/graph.gz` (immutable)
    2. Upload `latest.json` with sha + timestamp (overwrite)
    3. Read = always check `latest.json` first
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.review.settings import ReviewSettings, get_review_settings

logger = logging.getLogger(__name__)


@dataclass
class SnapshotMetadata:
    """`latest.json` pointer content."""

    commit_sha: str
    updated_at: str
    size_bytes: int
    sha256: str  # checksum of compressed snapshot


class SnapshotBackend(ABC):
    """Storage backend for repo graph snapshots."""

    @abstractmethod
    def upload_snapshot(
        self, repo_slug: str, commit_sha: str, source_path: Path,
    ) -> SnapshotMetadata:
        """Compress source_path → upload + update latest pointer."""

    @abstractmethod
    def download_latest(
        self, repo_slug: str, target_path: Path,
    ) -> SnapshotMetadata | None:
        """Resolve latest.json → download + decompress to target_path.

        Returns None if the snapshot does not exist.
        """

    @abstractmethod
    def list_snapshots(self, repo_slug: str) -> list[SnapshotMetadata]:
        """List all snapshots for the repo (sorted descending by updated_at)."""

    @abstractmethod
    def delete_snapshot(self, repo_slug: str, commit_sha: str) -> bool:
        """Delete a specific snapshot version. Does not touch latest.json."""


# ─── Compression helpers (stdlib gzip) ──────────────────────────


def _compress_file(source: Path, dest: Path) -> tuple[int, str]:
    """gzip-compress source → dest. Returns (size, sha256)."""
    sha = hashlib.sha256()
    size = 0
    with open(source, "rb") as src, gzip.open(dest, "wb", compresslevel=6) as out:
        while chunk := src.read(64 * 1024):
            out.write(chunk)
    # Compute sha256 of compressed file
    with open(dest, "rb") as f:
        while chunk := f.read(64 * 1024):
            sha.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


def _decompress_file(source: Path, dest: Path) -> None:
    with gzip.open(source, "rb") as src, open(dest, "wb") as out:
        while chunk := src.read(64 * 1024):
            out.write(chunk)


# ─── Local filesystem backend ───────────────────────────────────


class LocalSnapshotBackend(SnapshotBackend):
    """File-based snapshot storage. Default backend (no extra deps).

    Layout:
        {root}/
          {repo_slug}/
            {commit_sha}/graph.gz
            latest.json
    """

    SUFFIX = "graph.gz"

    def __init__(self, root: Path | None = None) -> None:
        settings = get_review_settings()
        self.root = root or settings.local_cache_dir / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)

    def upload_snapshot(
        self, repo_slug: str, commit_sha: str, source_path: Path,
    ) -> SnapshotMetadata:
        version_dir = self.root / repo_slug / commit_sha
        version_dir.mkdir(parents=True, exist_ok=True)
        target = version_dir / self.SUFFIX
        size, sha = _compress_file(source_path, target)

        meta = SnapshotMetadata(
            commit_sha=commit_sha,
            updated_at=datetime.now(UTC).isoformat(),
            size_bytes=size,
            sha256=sha,
        )

        # Atomic latest.json pointer
        pointer_path = self.root / repo_slug / "latest.json"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_pointer = pointer_path.with_suffix(".json.tmp")
        tmp_pointer.write_text(
            json.dumps(meta.__dict__, indent=2),
            encoding="utf-8",
        )
        tmp_pointer.replace(pointer_path)
        logger.info(
            "snapshot_uploaded backend=local repo=%s sha=%s size=%d",
            repo_slug, commit_sha, size,
        )
        return meta

    def download_latest(
        self, repo_slug: str, target_path: Path,
    ) -> SnapshotMetadata | None:
        pointer_path = self.root / repo_slug / "latest.json"
        if not pointer_path.exists():
            return None
        try:
            data = json.loads(pointer_path.read_text(encoding="utf-8"))
            meta = SnapshotMetadata(**data)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("local_snapshot_pointer_corrupt: %s", exc)
            return None

        snapshot_path = self.root / repo_slug / meta.commit_sha / self.SUFFIX
        if not snapshot_path.exists():
            logger.warning(
                "local_snapshot_missing repo=%s sha=%s",
                repo_slug, meta.commit_sha,
            )
            return None

        target_path.parent.mkdir(parents=True, exist_ok=True)
        _decompress_file(snapshot_path, target_path)
        logger.info(
            "snapshot_downloaded backend=local repo=%s sha=%s",
            repo_slug, meta.commit_sha,
        )
        return meta

    def list_snapshots(self, repo_slug: str) -> list[SnapshotMetadata]:
        repo_dir = self.root / repo_slug
        if not repo_dir.exists():
            return []
        out: list[SnapshotMetadata] = []
        for sha_dir in repo_dir.iterdir():
            if not sha_dir.is_dir():
                continue
            snapshot = sha_dir / self.SUFFIX
            if not snapshot.exists():
                continue
            stat = snapshot.stat()
            out.append(SnapshotMetadata(
                commit_sha=sha_dir.name,
                updated_at=datetime.fromtimestamp(
                    stat.st_mtime, tz=UTC
                ).isoformat(),
                size_bytes=stat.st_size,
                sha256="",  # not computed at list time
            ))
        out.sort(key=lambda m: m.updated_at, reverse=True)
        return out

    def delete_snapshot(self, repo_slug: str, commit_sha: str) -> bool:
        version_dir = self.root / repo_slug / commit_sha
        if not version_dir.exists():
            return False
        import shutil
        shutil.rmtree(version_dir, ignore_errors=True)
        return True


# ─── S3 backend (optional, gracefully missing) ──────────────────


class S3SnapshotBackend(SnapshotBackend):
    """S3-compatible backend (boto3-based, optional).

    Setup:
        pip install boto3
        export REVIEW_S3_BUCKET=my-bucket
        export REVIEW_S3_PREFIX=snapshots/

    AWS auth: standard boto3 chain (env vars / ~/.aws/credentials / IAM role).
    Works with MinIO or any other S3-compatible store (set endpoint_url via
    the `BOTO3_ENDPOINT_URL` env var or via the boto3 client config).
    """

    SUFFIX = "graph.gz"

    def __init__(
        self,
        bucket: str | None = None,
        prefix: str | None = None,
        region: str | None = None,
    ) -> None:
        try:
            import boto3  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "boto3 not installed. Install via `pip install boto3` "
                "or use LocalSnapshotBackend instead."
            ) from exc

        settings = get_review_settings()
        self.bucket = bucket or settings.s3_bucket
        if not self.bucket:
            raise ValueError("S3 bucket must be configured (REVIEW_S3_BUCKET)")
        self.prefix = (prefix or settings.s3_prefix).rstrip("/") + "/"
        self.region = region or settings.s3_region

    def _client(self):
        import boto3
        return boto3.client("s3", region_name=self.region)

    def _key(self, repo_slug: str, *parts: str) -> str:
        return self.prefix + "/".join([repo_slug, *parts])

    def upload_snapshot(
        self, repo_slug: str, commit_sha: str, source_path: Path,
    ) -> SnapshotMetadata:
        # 1. Compress locally first
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            size, sha = _compress_file(source_path, tmp_path)

            # 2. Upload immutable snapshot
            from boto3.s3.transfer import TransferConfig
            client = self._client()
            transfer_config = TransferConfig(
                multipart_threshold=8 * 1024 * 1024,
                max_concurrency=10,
                use_threads=True,
            )
            snapshot_key = self._key(repo_slug, commit_sha, self.SUFFIX)
            client.upload_file(
                str(tmp_path), self.bucket, snapshot_key,
                Config=transfer_config,
            )

            # 3. Update latest.json pointer (atomic last write)
            meta = SnapshotMetadata(
                commit_sha=commit_sha,
                updated_at=datetime.now(UTC).isoformat(),
                size_bytes=size,
                sha256=sha,
            )
            client.put_object(
                Bucket=self.bucket,
                Key=self._key(repo_slug, "latest.json"),
                Body=json.dumps(meta.__dict__).encode("utf-8"),
                ContentType="application/json",
            )
            logger.info(
                "snapshot_uploaded backend=s3 repo=%s sha=%s size=%d",
                repo_slug, commit_sha, size,
            )
            return meta
        finally:
            tmp_path.unlink(missing_ok=True)

    def download_latest(
        self, repo_slug: str, target_path: Path,
    ) -> SnapshotMetadata | None:
        client = self._client()
        try:
            pointer_resp = client.get_object(
                Bucket=self.bucket,
                Key=self._key(repo_slug, "latest.json"),
            )
            data = json.loads(pointer_resp["Body"].read().decode("utf-8"))
            meta = SnapshotMetadata(**data)
        except client.exceptions.NoSuchKey:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("s3_pointer_read_failed: %s", exc)
            return None

        snapshot_key = self._key(repo_slug, meta.commit_sha, self.SUFFIX)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            client.download_file(self.bucket, snapshot_key, str(tmp_path))
            target_path.parent.mkdir(parents=True, exist_ok=True)
            _decompress_file(tmp_path, target_path)
            logger.info(
                "snapshot_downloaded backend=s3 repo=%s sha=%s",
                repo_slug, meta.commit_sha,
            )
            return meta
        finally:
            tmp_path.unlink(missing_ok=True)

    def list_snapshots(self, repo_slug: str) -> list[SnapshotMetadata]:
        client = self._client()
        prefix = self._key(repo_slug)
        out: list[SnapshotMetadata] = []
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith("/" + self.SUFFIX):
                        continue
                    # Extract commit_sha from the key path
                    rel = key[len(prefix):].lstrip("/")
                    parts = rel.split("/")
                    if len(parts) < 2:
                        continue
                    commit_sha = parts[0]
                    out.append(SnapshotMetadata(
                        commit_sha=commit_sha,
                        updated_at=obj["LastModified"].isoformat(),
                        size_bytes=obj["Size"],
                        sha256="",
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("s3_list_failed: %s", exc)
            return []
        out.sort(key=lambda m: m.updated_at, reverse=True)
        return out

    def delete_snapshot(self, repo_slug: str, commit_sha: str) -> bool:
        client = self._client()
        key = self._key(repo_slug, commit_sha, self.SUFFIX)
        try:
            client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("s3_delete_failed: %s", exc)
            return False


# ─── Factory ────────────────────────────────────────────────────


def get_snapshot_backend(
    settings: ReviewSettings | None = None,
) -> SnapshotBackend:
    """Return the appropriate backend based on settings.

    Selection:
        S3 if REVIEW_S3_BUCKET set + boto3 available → S3SnapshotBackend
        Otherwise → LocalSnapshotBackend
    """
    settings = settings or get_review_settings()
    if settings.has_s3:
        try:
            return S3SnapshotBackend(
                bucket=settings.s3_bucket,
                prefix=settings.s3_prefix,
                region=settings.s3_region,
            )
        except (ImportError, ValueError, RuntimeError) as exc:
            logger.warning(
                "s3_backend_unavailable_falling_back_to_local err=%s", exc,
            )
    return LocalSnapshotBackend()
