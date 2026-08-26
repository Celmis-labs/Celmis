"""Tests для password hashing (scrypt стандартом, Argon2 якщо installed)."""

from __future__ import annotations

import pytest

from src.users.password import hash_password, verify_password


class TestPasswordHashing:
    def test_hash_creates_bytes(self) -> None:
        h = hash_password("MyP@ssw0rd!")
        assert isinstance(h, bytes)
        assert len(h) > 50  # serialized format

    def test_verify_correct_password(self) -> None:
        h = hash_password("hello world")
        assert verify_password("hello world", h) is True

    def test_verify_wrong_password(self) -> None:
        h = hash_password("correct")
        assert verify_password("wrong", h) is False

    def test_empty_password_raises(self) -> None:
        with pytest.raises(ValueError):
            hash_password("")

    def test_verify_empty_returns_false(self) -> None:
        h = hash_password("test")
        assert verify_password("", h) is False

    def test_verify_empty_hash_returns_false(self) -> None:
        assert verify_password("test", b"") is False

    def test_two_hashes_different_due_to_salt(self) -> None:
        """Same password — different hashes (salt-randomized)."""
        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        assert h1 != h2
        # Both verify
        assert verify_password("same-password", h1) is True
        assert verify_password("same-password", h2) is True

    def test_verify_unicode_password(self) -> None:
        h = hash_password("пароль-з-кирилицею-🔒")
        assert verify_password("пароль-з-кирилицею-🔒", h) is True
        assert verify_password("пароль-з-кирилицею", h) is False

    def test_verify_garbage_hash_returns_false(self) -> None:
        assert verify_password("password", b"not-a-valid-hash") is False
        assert verify_password("password", b"$scrypt$invalid") is False
