"""Tests for `src.llm.keys.resolve_api_key`.

Coverage:
    Fallback tiers:
        0. shared workspace key wins (keys are admin-managed, workspace-wide)
        1. user-scoped key — back-compat for rows saved before consolidation
        2. default-user key when user-specific missing (CLI fallback)
        3. env-var when nothing in store
        4. all four missing → LLMCredentialError with Connections hint

    Edge cases:
        5. Fernet decrypt failure → clean LLMCredentialError (not raw InvalidToken)
        6. Unknown provider → LLMCredentialError with `known:` list
        7. Placeholder value in DB or env → treated as absent (not returned)
        8. Secret never appears in exception messages
        9. Concurrent access — 5 threads resolving the same key
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from src.llm.keys import (
    WORKSPACE_KEY_USER,
    LLMCredentialError,
    has_key,
    resolve_api_key,
    workspace_slot,
)

# ─── Helpers ─────────────────────────────────────────────────────────


def _make_stored(secret: str):
    """Simulate `StoredCredentials` — the resolver only reads `.secret`."""
    m = MagicMock()
    m.secret = secret
    return m


@pytest.fixture
def mock_store():
    """Patch the credential store; return the MagicMock so tests can wire it up."""
    with patch("src.credentials.get_credential_store") as get_store:
        store = MagicMock()
        get_store.return_value = store
        yield store


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Blank out every env var the resolver looks at so tests are hermetic."""
    for var in [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "OPENROUTER_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY", "TOGETHER_API_KEY",
    ]:
        monkeypatch.delenv(var, raising=False)


# ─── Tier 0: the workspace's OWN slot (ws:{id}) is authoritative ────


def test_workspace_slot_wins_for_default(mock_store):
    """For the default tenant, a key in its own ws:default slot wins over the
    legacy shared 'workspace' slot and any personal row."""
    def load(provider, user_id, account_label):
        if user_id == workspace_slot("default"):
            return _make_stored("sk-ws-default-abcdef1234")
        if user_id == WORKSPACE_KEY_USER:
            return _make_stored("sk-legacy-workspace-xyz0")
        return _make_stored("sk-personal-should-not-win")

    mock_store.load.side_effect = load
    assert resolve_api_key("anthropic", user_id="u-123") == "sk-ws-default-abcdef1234"


def test_default_tenant_falls_back_to_legacy_workspace(mock_store):
    """Back-compat: the default tenant with no ws:default key still resolves the
    legacy shared 'workspace' slot so pre-isolation keys keep working."""
    def load(provider, user_id, account_label):
        if user_id == WORKSPACE_KEY_USER:
            return _make_stored("sk-legacy-workspace-abcdef")
        return None

    mock_store.load.side_effect = load
    assert resolve_api_key("anthropic", user_id="u-123") == "sk-legacy-workspace-abcdef"


def test_nondefault_workspace_reads_own_slot(mock_store):
    """A non-default workspace resolves its own ws:{id} slot."""
    def load(provider, user_id, account_label):
        if user_id == workspace_slot("acme"):
            return _make_stored("sk-acme-own-key-123456")
        return _make_stored("sk-should-not-be-used-000")

    mock_store.load.side_effect = load
    got = resolve_api_key("anthropic", user_id="u-123", workspace_id="acme")
    assert got == "sk-acme-own-key-123456"


def test_nondefault_workspace_is_isolated(mock_store):
    """SECURITY INVARIANT: a non-default workspace resolves ONLY its own
    ws:{id} slot — it must never fall through to the legacy shared 'workspace'
    slot or a personal row, even when those hold a valid key (cross-tenant
    credential leak the design critique flagged)."""
    def load(provider, user_id, account_label):
        if user_id == WORKSPACE_KEY_USER:
            return _make_stored("sk-other-tenant-must-not-leak")
        if user_id == "u-123":
            return _make_stored("sk-personal-must-not-leak-00")
        return None  # ws:acme itself has nothing saved

    mock_store.load.side_effect = load
    with pytest.raises(LLMCredentialError):
        resolve_api_key("anthropic", user_id="u-123", workspace_id="acme")


# ─── Tier 1: user-scoped key, back-compat for pre-consolidation rows ─


def test_user_scoped_key_used_when_no_workspace_key(mock_store):
    def load(provider, user_id, account_label):
        if user_id == "u-123":
            return _make_stored("sk-user-abcdef1234567890")
        return None

    mock_store.load.side_effect = load
    assert resolve_api_key("anthropic", user_id="u-123") == "sk-user-abcdef1234567890"


# ─── Tier 2: falls back to default user ─────────────────────────────


def test_falls_back_to_default_user(mock_store):
    def load(provider, user_id, account_label):
        return _make_stored("sk-cli-default-1234") if user_id == "default" else None

    mock_store.load.side_effect = load
    assert resolve_api_key("openai", user_id="u-abc") == "sk-cli-default-1234"


# ─── Tier 3: env fallback ───────────────────────────────────────────


def test_falls_back_to_env(mock_store, monkeypatch):
    mock_store.load.return_value = None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-fallback-abc")
    assert resolve_api_key("anthropic", user_id="anyone") == "sk-ant-env-fallback-abc"


def test_env_placeholder_ignored(mock_store, monkeypatch):
    mock_store.load.return_value = None
    monkeypatch.setenv("OPENAI_API_KEY", "replace-me")
    with pytest.raises(LLMCredentialError) as exc:
        resolve_api_key("openai")
    assert "OPENAI_API_KEY" in str(exc.value)
    assert "Connections" in str(exc.value)


# ─── All three missing → clean error ────────────────────────────────


def test_all_three_missing_raises(mock_store):
    mock_store.load.return_value = None
    with pytest.raises(LLMCredentialError) as exc:
        resolve_api_key("google", user_id="ghost-user")
    msg = str(exc.value)
    assert "google" in msg
    assert "Connections" in msg
    assert "GEMINI_API_KEY" in msg  # the env var name for google


# ─── Edge cases ──────────────────────────────────────────────────────


def test_fernet_decrypt_failure_surfaces_clean_error(mock_store):
    """Corrupted encrypted_secret — resolver must re-raise as LLMCredentialError
    with a specific message, not surface the InvalidToken from cryptography."""
    from src.credentials.store import CredentialStoreError

    mock_store.load.side_effect = CredentialStoreError(
        "Cannot decrypt credentials — master key changed or DB corrupted"
    )
    with pytest.raises(LLMCredentialError) as exc:
        resolve_api_key("anthropic", user_id="u-123")
    msg = str(exc.value)
    assert "corrupted" in msg.lower()
    assert "provider='anthropic'" in msg
    # Original exception hangs off __cause__ but must NOT appear in main message.
    assert "InvalidToken" not in msg


def test_unknown_provider_lists_valid_ones(mock_store):
    with pytest.raises(LLMCredentialError) as exc:
        resolve_api_key("cohere")  # not in _ENV_FALLBACK
    msg = str(exc.value)
    assert "cohere" in msg
    assert "unsupported" in msg
    # Must enumerate valid options so the caller knows what's supported.
    assert "anthropic" in msg
    assert "openrouter" in msg


def test_placeholder_in_store_falls_through_to_env(mock_store, monkeypatch):
    """User's `credentials_v2` row has a `.env`-style placeholder — should be
    ignored, not returned. Env var wins."""
    def load(provider, user_id, account_label):
        return _make_stored("replace-me")

    mock_store.load.side_effect = load
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-actual-key-abc123")
    assert resolve_api_key("anthropic", user_id="u-1") == "sk-ant-actual-key-abc123"


def test_secret_never_leaks_into_exception(mock_store, monkeypatch):
    """Even if we're in an error path, the resolver must not embed the raw
    secret in exception text (repr or str)."""
    secret = "sk-VERY-SECRET-DO-NOT-LEAK-abc123"
    mock_store.load.return_value = _make_stored(secret)
    # Force resolver into an error path — unknown provider.
    with pytest.raises(LLMCredentialError) as exc:
        resolve_api_key("nonexistent-provider")
    assert secret not in str(exc.value)
    assert secret not in repr(exc.value)


def test_concurrent_access(mock_store):
    """5 threads resolving the same key simultaneously. Must all get the same
    value with no race / deadlock (sqlite3 cursor is not thread-safe if shared,
    but store.load acquires a fresh connection each call)."""
    call_count = [0]

    def load(provider, user_id, account_label):
        call_count[0] += 1
        return _make_stored("sk-shared-value-abcdefgh")

    mock_store.load.side_effect = load
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(
            resolve_api_key,
            ["anthropic"] * 5,
            ["u-1"] * 5,
        ))
    assert all(r == "sk-shared-value-abcdefgh" for r in results)
    # Sanity — store was actually consulted 5 times (not memoised globally).
    assert call_count[0] == 5


# ─── has_key() convenience ──────────────────────────────────────────


def test_has_key_returns_false_when_missing(mock_store):
    mock_store.load.return_value = None
    assert has_key("anthropic", user_id="ghost") is False


def test_has_key_returns_true_when_env_set(mock_store, monkeypatch):
    mock_store.load.return_value = None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-abcdefghij")
    assert has_key("openai") is True
