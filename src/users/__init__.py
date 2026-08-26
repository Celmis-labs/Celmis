"""App user database — foundation for multi-user MCP + multi-tenant credentials.

Stage 16.1 (May 2026):
    User model with email + scrypt password hash + optional Google OAuth
    subject.
    SQLite (default) + Postgres (via SQLAlchemy URL) backends.

Architecture (single-user mode by default):
    - the 'default' user is created automatically on first init
    - Multi-user enabled when the DB has > 1 user OR via an env flag

Stage 16 — implemented user model + scrypt hashing + UserStore CRUD.
"""

from src.users.models import User, UserAuthMethod
from src.users.password import hash_password, verify_password
from src.users.store import (
    DEFAULT_USER_ID,
    UserExistsError,
    UserNotFoundError,
    UserStore,
    get_user_store,
)

__all__ = [
    "DEFAULT_USER_ID",
    "User",
    "UserAuthMethod",
    "UserExistsError",
    "UserNotFoundError",
    "UserStore",
    "get_user_store",
    "hash_password",
    "verify_password",
]
