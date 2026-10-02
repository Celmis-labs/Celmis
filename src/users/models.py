"""User data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class UserAuthMethod(StrEnum):
    """How the user authenticated to the App."""

    PASSWORD = "password"          # email + scrypt hash
    GOOGLE_OAUTH = "google_oauth"  # Google subject (no password stored)
    BOTH = "both"                  # password set + Google linked
    OIDC = "oidc"                  # generic OIDC / Keycloak subject


@dataclass
class User:
    """Application user.

    Identity options:
        email + password (Argon2/scrypt hash)
        Google OAuth (sub claim — stable Google user ID)
        Or both (linked account)
        OIDC / Keycloak (iss + sub — stable per issuer); may be linked to
        any of the above, `auth_method` keeps describing the first one

    Permissions:
        is_admin — can manage other users + system-wide settings
        scopes   — default scopes for issued JWT tokens
    """

    id: str  # UUID or 'default' for single-user mode
    email: str
    auth_method: UserAuthMethod = UserAuthMethod.PASSWORD
    password_hash: bytes | None = None  # None for google-only
    google_sub: str | None = None  # OAuth subject ID — stable
    is_admin: bool = False
    is_active: bool = True
    scopes: list[str] = field(
        default_factory=lambda: ["read:graph", "read:groups"])
    created_at: str = ""  # ISO timestamp
    last_login_at: str | None = None
    name: str = ""  # display name
    oidc_iss: str | None = None  # OIDC issuer URL
    oidc_sub: str | None = None  # OIDC subject — stable per issuer

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    @property
    def has_password(self) -> bool:
        return self.password_hash is not None

    @property
    def has_google(self) -> bool:
        return bool(self.google_sub)

    @property
    def has_oidc(self) -> bool:
        return bool(self.oidc_sub)
