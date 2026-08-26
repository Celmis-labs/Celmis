"""Vector-store client factory + the tenant scope every query must carry.

Resolution order for the CLIENT:
    1. UI-saved config (credential store, provider="vector_store", global
       default slot — the SERVER is one shared installation resource, so its
       address is installation-level, not per-workspace).
    2. Settings/env: QDRANT_URL (+ optional QDRANT_API_KEY).
    3. EMBEDDED local Qdrant persisted at workspace_dir/qdrant_local —
       zero-config default so a fresh install needs no cloud account.

The stored config is provider-shaped ({"type": "...", ...}) so Pinecone /
Weaviate adapters can plug in behind the same setting later; today "local"
and "qdrant" (any URL, incl. Cloud) are implemented.

Tenancy
-------
One shared server and one collection per kind does NOT mean one tenant. Every
point carries ``workspace_id`` in its payload, stamped at write time by the
caller that knows the tenant, and every read goes through ``VectorScope``:

    global admin      no condition — the whole collection
    a workspace       ``workspace_id == theirs``, ANDed into `must`
    no tenant         a condition that cannot match, so the read is empty

That last row is the one that matters. A caller whose workspace normalizes
away (blank, or the ``"default"`` placeholder that arrives when nobody said
which tenant this is) gets a scope matching NOTHING rather than everything —
same reasoning, and the same `normalize_workspace_id`, as the audit reader in
src/api/routers/audit.py. `global_` is therefore a separate flag: collapsing
it into "workspace_id is None means no filter" turns a tenant-less caller into
a read of the whole installation.

Points written before this field existed have no ``workspace_id`` at all. A
`must` equality condition never matches a missing key, so they are already
invisible to every tenant scope and visible only to a global one — they do not
have to be found and deleted to be safe. scripts/backfill_vector_workspaces.py
gives them their real owner where the repo→workspace mapping knows one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.security.audit import normalize_workspace_id

logger = logging.getLogger(__name__)

#: Payload key holding the tenant, in every collection this module serves.
VECTOR_WORKSPACE_KEY = "workspace_id"

#: The value a "matches nothing" scope filters on. Nothing writes it — that is
#: the whole point. Expressing "no results" as a real filter rather than an
#: early `return []` keeps ONE enforcement path: a caller cannot skip the guard
#: by skipping the short-circuit, because there is no short-circuit to skip.
_UNMATCHABLE_WORKSPACE = "\x00celmis:no-tenant\x00"


@dataclass(frozen=True)
class VectorScope:
    """Whose points a search may return.

    Build one with :meth:`for_workspace` (a tenant) or :meth:`global_admin`
    (the installation). There is deliberately no default constructor value:
    every search states whose data it is searching.
    """

    global_: bool
    workspace_id: str | None

    @classmethod
    def for_workspace(cls, workspace_id: str | None) -> VectorScope:
        """The scope of one tenant. A workspace that normalizes to None —
        blank, or the ``"default"`` placeholder — matches nothing."""
        ws = normalize_workspace_id(workspace_id)
        if ws is None:
            logger.warning(
                "vector_scope_untenanted raw=%r — search will return nothing",
                workspace_id,
            )
        return cls(global_=False, workspace_id=ws)

    @classmethod
    def global_admin(cls) -> VectorScope:
        """Every tenant, plus the points that belong to nobody. Only for a
        global admin, the backfill, and installation-wide maintenance."""
        return cls(global_=True, workspace_id=None)

    @property
    def matches_nothing(self) -> bool:
        return not self.global_ and self.workspace_id is None

    def must_conditions(self) -> list[Any]:
        """Conditions to AND into a Qdrant filter's ``must``.

        THIS is the isolation boundary — every read path splices the result
        into its own `must` list, so there is exactly one place where "which
        tenant" is decided.
        """
        from qdrant_client import models

        if self.global_:
            return []
        return [models.FieldCondition(
            key=VECTOR_WORKSPACE_KEY,
            match=models.MatchValue(
                value=self.workspace_id or _UNMATCHABLE_WORKSPACE,
            ),
        )]

    def allows(self, payload: Mapping[str, Any] | None) -> bool:
        """Same decision, in Python — for results that came back from a path
        without a server-side filter (a fake client in tests, a store that
        cannot filter). Not a substitute for `must_conditions`."""
        if self.global_:
            return True
        ws = normalize_workspace_id((payload or {}).get(VECTOR_WORKSPACE_KEY))
        if ws is None:
            return False
        return ws == self.workspace_id


def stamp_workspace(payload: dict[str, Any], workspace_id: str | None) -> dict[str, Any]:
    """Payload plus its tenant — the write-time half of the boundary.

    A workspace that normalizes to None is written with NO tenant key rather
    than with the literal ``"default"``. Both readings are honest about the
    same fact ("we do not know whose this is"), but only one of them keeps the
    point out of whichever tenant happens to own the seeded 'default'
    workspace. An unattributed point stays admin-only until the backfill can
    name its owner.
    """
    ws = normalize_workspace_id(workspace_id)
    if ws is None:
        logger.warning(
            "vector_write_unattributed raw=%r — point will be visible to "
            "global admins only until it is backfilled", workspace_id,
        )
        return dict(payload)
    return {**payload, VECTOR_WORKSPACE_KEY: ws}


_PROVIDER_TAG = "vector_store"
_SLOT = "ws:default"          # installation-level (matches workspace_slot("default"))

_lock = threading.Lock()
_cached_client: Any | None = None
_cached_sig: str | None = None

SUPPORTED_TYPES = ("local", "qdrant")
PLANNED_TYPES = ("pinecone", "weaviate")


def load_store_config() -> dict[str, Any]:
    """Effective vector-store config (never raises).

    Shape: {"type": "local"|"qdrant", "url": str, "api_key_set": bool,
            "source": "ui"|"env"|"default"}.
    """
    try:
        from src.credentials import get_credential_store
        row = get_credential_store().load(
            provider=_PROVIDER_TAG, user_id=_SLOT, account_label="default",
        )
        if row is not None and row.secret:
            cfg = json.loads(row.secret)
            if cfg.get("type") in SUPPORTED_TYPES:
                return {
                    "type": cfg["type"],
                    "url": cfg.get("url", ""),
                    "api_key": cfg.get("api_key", ""),
                    "api_key_set": bool(cfg.get("api_key")),
                    "source": "ui",
                }
    except Exception as exc:  # noqa: BLE001
        logger.debug("vector_store_config_unreadable err=%s", exc)

    from src.config import get_settings
    s = get_settings()
    if s.qdrant_url:
        return {
            "type": "qdrant", "url": s.qdrant_url,
            "api_key": s.qdrant_api_key.get_secret_value() if s.qdrant_api_key else "",
            "api_key_set": bool(s.qdrant_api_key), "source": "env",
        }
    return {"type": "local", "url": "", "api_key": "", "api_key_set": False,
            "source": "default"}


def save_store_config(*, type_: str, url: str = "", api_key: str = "",
                      saved_by: str = "") -> None:
    if type_ not in SUPPORTED_TYPES:
        raise ValueError(
            f"vector store type {type_!r} not supported yet "
            f"(supported: {SUPPORTED_TYPES}, planned: {PLANNED_TYPES})"
        )
    from src.credentials import get_credential_store
    get_credential_store().save(
        provider=_PROVIDER_TAG,
        secret=json.dumps({"type": type_, "url": url, "api_key": api_key}),
        metadata={"saved_by": saved_by},
        user_id=_SLOT, account_label="default",
    )
    reset_client()


def clear_store_config() -> None:
    from src.credentials import get_credential_store
    get_credential_store().delete(
        provider=_PROVIDER_TAG, user_id=_SLOT, account_label="default",
    )
    reset_client()


def get_vector_client():
    """Shared QdrantClient for the effective config. Cached — embedded local
    mode allows only ONE open handle per path, so every caller must share it."""
    global _cached_client, _cached_sig
    cfg = load_store_config()
    sig = f"{cfg['type']}:{cfg['url']}:{'k' if cfg['api_key_set'] else ''}"
    with _lock:
        if _cached_client is not None and _cached_sig == sig:
            return _cached_client
        from qdrant_client import QdrantClient

        if cfg["type"] == "qdrant" and cfg["url"]:
            client = QdrantClient(
                url=cfg["url"],
                api_key=cfg["api_key"] or None,
                timeout=30,
            )
        else:
            from src.config import get_settings
            path = get_settings().workspace_dir / "qdrant_local"
            path.mkdir(parents=True, exist_ok=True)
            client = QdrantClient(path=str(path))
            logger.info("vector_store_embedded path=%s", path)
        _cached_client = client
        _cached_sig = sig
        return client


def reset_client() -> None:
    global _cached_client, _cached_sig
    with _lock:
        old = _cached_client
        _cached_client = None
        _cached_sig = None
    if old is not None:
        # The handle is already detached; a failing close must not block reset.
        with contextlib.suppress(Exception):
            old.close()


def test_connection(*, type_: str, url: str = "", api_key: str = "") -> dict[str, Any]:
    """Ping a candidate config without saving it."""
    from qdrant_client import QdrantClient
    try:
        if type_ == "qdrant" and url:
            client = QdrantClient(url=url, api_key=api_key or None, timeout=10)
        elif type_ == "local":
            from src.config import get_settings
            path = get_settings().workspace_dir / "qdrant_local"
            path.mkdir(parents=True, exist_ok=True)
            # Never open a second embedded handle while the shared one is live.
            cfg = load_store_config()
            client = get_vector_client() if cfg["type"] == "local" else QdrantClient(path=str(path))
        else:
            return {"ok": False, "detail": f"type {type_!r} not supported yet"}
        collections = client.get_collections().collections
        return {"ok": True, "detail": f"reachable — {len(collections)} collection(s)",
                "collections": [c.name for c in collections]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc)[:300]}


__all__ = [
    "VECTOR_WORKSPACE_KEY",
    "VectorScope",
    "stamp_workspace",
    "get_vector_client",
    "reset_client",
    "load_store_config",
    "save_store_config",
    "clear_store_config",
    "test_connection",
    "SUPPORTED_TYPES",
    "PLANNED_TYPES",
]
