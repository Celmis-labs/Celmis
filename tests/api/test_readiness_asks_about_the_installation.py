"""Readiness reports whether ANY tenant can reach a model.

THE DEFECT. `/readyz` probed exactly one workspace: `_load_workspace_config()`
takes `workspace_id="default"`, and on a multi-tenant deployment every real
key lives under `ws:{id}`. On production it therefore answered

    "llm_config": {"ok": false, "provider": null, "model": null}

on a system that had just spent $0.94 on model calls, with five workspaces and
a working LiteLLM gateway. A readiness field that is false forever is a field
an operator learns to ignore, which is worse than not having it — the rest of
`/readyz` is genuine (it probes Postgres, Qdrant, the user store and the
review store), and one permanently-red line teaches people to skim past all
of them.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from src.credentials.store import CredentialStore

    s = CredentialStore(db_path=tmp_path / "cred.db", master_key=Fernet.generate_key())
    monkeypatch.setattr("src.credentials.get_credential_store", lambda: s)
    return s


def test_the_store_can_enumerate_slots(store):
    store.save(provider="llm_config", secret="{}", user_id="ws:one",
               account_label="workspace")
    store.save(provider="llm_config", secret="{}", user_id="ws:two",
               account_label="workspace")
    store.save(provider="github", secret="x", user_id="ws:one",
               account_label="default")

    slots = store.slots_with(provider="llm_config", account_label="workspace")

    assert slots == ["ws:one", "ws:two"]


def test_enumeration_returns_no_secret(store):
    """Presence only. The probe must not decrypt anything to answer."""
    store.save(provider="llm_config", secret="sk-super-secret", user_id="ws:one",
               account_label="workspace")

    assert store.slots_with(provider="llm_config") == ["ws:one"]


def test_a_different_account_label_is_not_counted(store):
    store.save(provider="llm_config", secret="{}", user_id="ws:one",
               account_label="something-else")

    assert store.slots_with(
        provider="llm_config", account_label="workspace") == []


def test_an_empty_store_is_an_empty_list(store):
    assert store.slots_with(provider="llm_config") == []


def test_the_probe_never_breaks_readiness(monkeypatch):
    """A readiness check must not be the thing that breaks readiness."""
    from src.api.main import _workspaces_with_llm_config

    def boom():
        raise RuntimeError("store on fire")

    monkeypatch.setattr("src.credentials.get_credential_store", boom)

    assert _workspaces_with_llm_config() == []


def test_the_probe_says_which_answer_you_are_reading():
    """`scope` distinguishes "this workspace's provider" from "some workspace
    has one", so a single-tenant install still sees its provider name and a
    multi-tenant one is not handed a number it has to interpret."""
    import ast
    import inspect

    from src.api import main

    body = ast.unparse(ast.parse(inspect.getsource(main)))
    assert "'scope': 'default'" in body
    assert "'scope': 'any_workspace'" in body
