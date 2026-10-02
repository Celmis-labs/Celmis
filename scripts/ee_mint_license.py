"""Mint a Celmis Enterprise licence — an EdDSA (Ed25519) JWT.

    python scripts/ee_mint_license.py --customer "Acme Corp" \
        --features sso,analytics --days 365 \
        --key ~/.celmis-ee/license-signing-key.pem

Prints the token on stdout and nothing else, so it can be piped straight into
a secret store. The private key is read from `--key` and never printed; it
belongs outside this repository (the public half is embedded in
src/ee/license.py, which is the only place a licence is verified).

The claims are exactly what `src.ee.license.verify` requires: iss
"celmis-licensing", sub (the customer), features, iat, exp, and nbf when
`--not-before` is given.

This script is AGPL: minting is not the enterprise feature, and anybody can
run it — a token signed with any key but the owner's does not verify.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ISSUER = "celmis-licensing"
#: Kept in step with src/ee/license.py KNOWN_FEATURES by
#: tests/ee/test_the_licence_gate.py, rather than imported: this script must
#: run on a machine that has only the script and the key.
KNOWN_FEATURES = ("sso", "analytics")


def mint(
    private_key: Ed25519PrivateKey,
    *,
    customer: str,
    features: Iterable[str],
    days: int,
    now: datetime | None = None,
    not_before: datetime | None = None,
) -> str:
    """Sign a licence. Raises ValueError on input that could not verify."""
    customer = customer.strip()
    if not customer:
        raise ValueError("--customer is required")
    feats = sorted({f.strip() for f in features if f.strip()})
    if not feats:
        raise ValueError("at least one feature is required")
    unknown = sorted(set(feats) - set(KNOWN_FEATURES))
    if unknown:
        raise ValueError(f"unknown feature(s): {', '.join(unknown)}; "
                         f"known: {', '.join(KNOWN_FEATURES)}")
    if days <= 0:
        raise ValueError("--days must be positive")
    issued = (now or datetime.now(UTC)).replace(microsecond=0)
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": customer,
        "features": feats,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(days=days)).timestamp()),
    }
    if not_before is not None:
        claims["nbf"] = int(not_before.timestamp())
    return jwt.encode(claims, private_key, algorithm="EdDSA")


def _load_key(path: str) -> Ed25519PrivateKey:
    data = Path(path).expanduser().read_bytes()
    key = load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("the signing key is not an Ed25519 private key")
    return key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--customer", required=True)
    ap.add_argument("--features", required=True, help="comma-separated, e.g. sso,analytics")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--key", required=True, help="path to the Ed25519 private key (PEM)")
    ap.add_argument("--not-before", help="ISO date the licence starts (optional)")
    args = ap.parse_args(argv)
    try:
        nbf = datetime.fromisoformat(args.not_before) if args.not_before else None
        if nbf is not None and nbf.tzinfo is None:
            nbf = nbf.replace(tzinfo=UTC)
        token = mint(_load_key(args.key), customer=args.customer,
                     features=args.features.split(","), days=args.days,
                     not_before=nbf)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
