"""Encrypted credential storage.

Stores Bitbucket / other credentials in SQLite, encrypted via Fernet
(AES-128-CBC + HMAC).
Master key — from the ENV var `CREDENTIAL_MASTER_KEY` or an auto-generated
file with chmod 600.
"""

from src.credentials.git_keys import (
    GIT_PROVIDERS,
    WORKSPACE_GIT_USER,
    git_workspace_slot,
    resolve_git_credential,
)
from src.credentials.store import (
    CredentialStore,
    CredentialStoreError,
    StoredCredentials,
    get_credential_store,
    get_or_create_master_key,
)

__all__ = [
    "GIT_PROVIDERS",
    "WORKSPACE_GIT_USER",
    "git_workspace_slot",
    "CredentialStore",
    "CredentialStoreError",
    "StoredCredentials",
    "get_credential_store",
    "get_or_create_master_key",
    "resolve_git_credential",
]
