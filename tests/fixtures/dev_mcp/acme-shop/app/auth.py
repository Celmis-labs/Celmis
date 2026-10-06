"""Bearer-token authentication for the shop API."""

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)


def verify_token(token: str) -> dict:
    """Check the signature against the issuer's JWKS and return the claims."""
    s = get_settings()
    signing_key = PyJWKClient(s.jwt_jwks_url).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=s.jwt_audience,
        issuer=s.jwt_issuer,
    )


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    """FastAPI dependency: the authenticated caller, or 401."""
    if creds is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return verify_token(creds.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
