"""``world.materialize``: fresh canaries per run, secret files committed in a temp repo only."""

from __future__ import annotations

import subprocess

import pytest

from tests.e2e_local import world as W


@pytest.fixture(scope="module")
def two_worlds(tmp_path_factory):
    return (W.materialize(tmp_path_factory.mktemp("w1")), W.materialize(tmp_path_factory.mktemp("w2")))


def _tracked(path) -> list[str]:
    return subprocess.run(["git", "-C", str(path), "ls-files"], capture_output=True, text=True,
                          check=True).stdout.splitlines()


def test_every_run_gets_different_canaries(two_worlds) -> None:
    a, b = two_worlds
    assert a.canaries.keys() == b.canaries.keys()
    assert all(a.canaries[k] != b.canaries[k] for k in a.canaries)


def _in_repo(repo, needle: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "grep", "-q", "-F", needle]).returncode == 0


def test_a_canary_is_findable_by_one_substring_search(two_worlds) -> None:
    """Otherwise a clean leak scan would prove nothing about that canary."""
    w, _ = two_worlds
    for label, value in w.canaries.items():
        forms = (value, W.base64.b64encode(value.encode()).decode())
        assert any(_in_repo(r, f) for r in w.repos.values() for f in forms), label


def test_the_secret_files_are_committed_in_the_temporary_repository(two_worlds) -> None:
    w, _ = two_worlds
    for name, rels in w.secret_paths.items():
        tracked = _tracked(w.repos[name])
        for rel in rels:
            assert rel in tracked, f"{name}: {rel} was not committed, so the indexer would never see it"


def test_the_committed_code_placeholder_is_replaced_by_the_canary(two_worlds) -> None:
    w, _ = two_worlds
    text = (w.repos["acme/shop"] / "app" / "legacy.py").read_text()
    assert w.canaries["DSN_PASSWORD"] in text and "__CANARY_" not in text


def test_the_pem_files_are_shaped_like_keys_and_the_aws_canary_like_an_access_key(two_worlds) -> None:
    w, _ = two_worlds
    pem = (w.repos["acme/billing"] / "deploy" / "server.key").read_text()
    assert pem.startswith("-----BEGIN PRIVATE KEY-----") and w.canaries["BILLING_PEM"] in pem
    assert w.canaries["AWS_KEY"].startswith("AKIA") and len(w.canaries["AWS_KEY"]) == 20


def test_the_k8s_secret_carries_the_value_base64_encoded_as_kubernetes_does(two_worlds) -> None:
    w, _ = two_worlds
    text = (w.repos["acme/billing"] / "deploy" / "k8s" / "secret.yaml").read_text()
    assert w.canaries["K8S_SECRET"] not in text
    assert W.base64.b64encode(w.canaries["K8S_SECRET"].encode()).decode() in text
    assert w.leaks_in(text) == ["K8S_SECRET"]


def test_leaks_in_reports_labels_never_values(two_worlds) -> None:
    w, _ = two_worlds
    found = w.leaks_in(f"x {w.canaries['STRIPE']} y")
    assert found == ["STRIPE"] and all(w.canaries[k] not in k for k in found)


def test_the_source_tree_is_never_written_to(two_worlds) -> None:
    status = subprocess.run(["git", "-C", str(W.ROOT), "status", "--porcelain", "--",
                             "tests/fixtures/dev_mcp"], capture_output=True, text=True).stdout
    assert "CANARY" not in status
    assert not list(W.FIXTURES.rglob(".env")) and not list(W.FIXTURES.rglob("id_rsa"))


def test_materialising_inside_the_repository_is_refused() -> None:
    with pytest.raises(ValueError, match="inside the source tree"):
        W.materialize(W.ROOT / "tests" / "fixtures" / "dev_mcp_out")
