# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""A throwaway licensing keypair for tests.

Every test that needs a licence signs one with a key generated here and
points `src.ee.license.PUBLIC_KEY_PEM` at its public half. Nothing in the
test suite reads, or could produce a token for, the real key — a token minted
here does NOT verify against the embedded public key, which is itself
asserted in tests/ee/test_the_licence_gate.py.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from scripts.ee_mint_license import mint

TEST_KEY = Ed25519PrivateKey.generate()
TEST_PUBLIC_PEM = TEST_KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo,
).decode("ascii")

CUSTOMER = "Test Customer Ltd"


def mint_test_license(
    features: Iterable[str] = ("sso", "analytics"),
    *,
    days: int = 30,
    customer: str = CUSTOMER,
    now: datetime | None = None,
    not_before: datetime | None = None,
    key: Ed25519PrivateKey = TEST_KEY,
) -> str:
    return mint(key, customer=customer, features=features, days=days,
                now=now, not_before=not_before)


def trust_test_key(mp) -> None:
    """Make src.ee.license accept tokens signed with TEST_KEY. `mp` is a
    pytest MonkeyPatch, so the real key comes back when the test ends."""
    from src.ee import license as lic

    mp.setattr(lic, "PUBLIC_KEY_PEM", TEST_PUBLIC_PEM)


def isolate_license_store(mp, directory) -> object:
    """Point the UI licence slot (src/ee/license_store.py) at an empty,
    throwaway credential store under `directory`, so no test reads — or
    writes — the developer's real one. Returns the store."""
    from cryptography.fernet import Fernet

    from src.credentials.store import CredentialStore
    from src.ee import license_store

    store = CredentialStore(Path(directory) / "credentials.db", Fernet.generate_key())
    mp.setattr(license_store, "get_store", lambda: store)
    return store
