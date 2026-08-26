"""Password hashing — scrypt (stdlib hashlib).

Strategy: scrypt — Python stdlib, no external dependencies, RFC 7914.
Production-grade memory-hard function (resistant to ASIC/GPU attacks).

Format: '$scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>' — versionable, parseable.
Default parameters (Python 3.13 docs recommended Apr 2026):
    n = 2**16 = 65536  (CPU/memory cost)
    r = 8              (block size)
    p = 1              (parallelization)
    salt = 16 bytes random
    output = 64 bytes hash

If argon2-cffi is installed — use it instead (Argon2id is the PHC winner,
better in practice). Graceful fallback if argon2 is missing (more common).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Defaults per scrypt RFC + 2026 hardware
SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_DKLEN = 64

_SCRYPT_PREFIX = "$scrypt$"


def _check_argon2() -> bool:
    """Optional: prefer Argon2id if argon2-cffi is installed."""
    try:
        import argon2  # noqa: F401
        return True
    except ImportError:
        return False


def hash_password(password: str) -> bytes:
    """Hash plaintext password. Returns serialized bytes.

    Uses argon2-cffi if installed (Argon2id), otherwise scrypt (stdlib).
    Format-versioned: parse_password_hash() detects algorithm.
    """
    if not password:
        raise ValueError("password cannot be empty")
    if _check_argon2():
        from argon2 import PasswordHasher
        ph = PasswordHasher()
        return ph.hash(password).encode("utf-8")
    return _scrypt_hash(password)


def verify_password(password: str, hash_data: bytes) -> bool:
    """Verify password against stored hash. Returns True/False (constant-time).

    Auto-detects the algorithm from the hash format.
    """
    if not password or not hash_data:
        return False

    hash_str = hash_data.decode("utf-8", errors="replace")

    if hash_str.startswith("$argon2"):
        if not _check_argon2():
            # Hash created with argon2 but argon2 is not available now
            return False
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError
        ph = PasswordHasher()
        try:
            ph.verify(hash_str, password)
            return True
        except VerifyMismatchError:
            return False
        except Exception:  # noqa: BLE001
            return False

    if hash_str.startswith(_SCRYPT_PREFIX):
        return _scrypt_verify(password, hash_str)

    return False


# ─── Scrypt implementation ─────────────────────────────────────────


def _scrypt_hash(
    password: str,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> bytes:
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n, r=r, p=p,
        dklen=SCRYPT_DKLEN,
        maxmem=512 * 1024 * 1024,  # 512 MB ceiling — protection against OOM
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")
    return f"{_SCRYPT_PREFIX}{n}${r}${p}${salt_b64}${hash_b64}".encode()


def _scrypt_verify(password: str, hash_str: str) -> bool:
    parts = hash_str.split("$")
    # Expected: ['', 'scrypt', n, r, p, salt_b64, hash_b64]
    if len(parts) != 7 or parts[1] != "scrypt":
        return False
    try:
        n, r, p = int(parts[2]), int(parts[3]), int(parts[4])
        salt = base64.b64decode(parts[5])
        expected = base64.b64decode(parts[6])
    except (ValueError, base64.binascii.Error):
        return False

    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n, r=r, p=p,
            dklen=len(expected),
            maxmem=512 * 1024 * 1024,
        )
    except (ValueError, MemoryError):
        return False

    # Constant-time compare to avoid timing attacks
    return hmac.compare_digest(derived, expected)
