"""Path templates: `{id}`, `:id`, `<int:id>` and `*` are the same wildcard."""

from __future__ import annotations

import pytest

from src.indexing.graph.xrepo import methods_compatible, normalize_path, paths_match


@pytest.mark.parametrize("raw,expected", [
    ("/invoices/{id}/pay", "/invoices/{}/pay"),
    ("/invoices/:id/pay", "/invoices/{}/pay"),
    ("/invoices/<int:id>/pay", "/invoices/{}/pay"),
    ("/invoices/*/pay", "/invoices/{}/pay"),
    ("invoices//{id}/pay/?x=1#top", "/invoices/{}/pay"),
    ("/files/{path:path}", "/files/{**}"),
    ("/files/<path:rest>/ignored", "/files/{**}"),
    ("/static/**", "/static/{**}"),
    ("/", "/"),
    ("/v1/file-{name}.json", "/v1/file-{}.json"),
])
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("route,call,how", [
    ("/invoices/{}/pay", "/invoices/{}/pay", "exact"),
    ("/invoices/{}/pay", "/invoices/42/pay", "exact"),
    ("/invoices/42/pay", "/invoices/{}/pay", "exact"),
    ("/files/{**}", "/files/a/b/c", "exact"),
    ("/v1/file-{}.json", "/v1/file-7.json", "exact"),
    ("/invoices/{}/pay", "/v2/invoices/{}/pay", "suffix"),
    ("/invoices/{}/pay", "/api/v2/invoices/9/pay", "suffix"),
    ("/invoices/{}/pay", "/invoices/{}/refund", None),
    ("/invoices/{}/pay", "/invoices/{}", None),
    ("/invoices", "/orders/{}", None),
    ("/{}", "/api/anything", None),
    ("/{}/{}", "/api/x/y", None),
])
def test_paths_match(route: str, call: str, how: str | None) -> None:
    assert paths_match(route, call) == how


def test_a_route_that_is_all_wildcards_is_never_a_suffix_match() -> None:
    assert paths_match("/{}", "/v1/orders") is None


@pytest.mark.parametrize("route,call,ok", [
    ("POST", "POST", True), ("post", "POST", True), ("POST", "GET", False),
    ("*", "DELETE", True), ("GET", "", True), ("", "PUT", True),
])
def test_methods(route: str, call: str, ok: bool) -> None:
    assert methods_compatible(route, call) is ok
