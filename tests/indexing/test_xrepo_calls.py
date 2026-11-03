"""The outgoing HTTP calls of a file, read through the real language adapters."""

from __future__ import annotations

from pathlib import Path

from src.indexing.graph.languages.go import GoExtractor
from src.indexing.graph.languages.java import JavaExtractor
from src.indexing.graph.languages.python import PythonExtractor
from src.indexing.graph.languages.typescript import TypeScriptExtractor


def _calls(extractor, rel: str, source: str) -> list[tuple[str, str, str, str]]:
    res = extractor.extract(Path(rel), source=source.encode())
    assert res.facts is not None
    return [(c.method, c.host or c.host_hint, c.path, c.caller) for c in res.facts.calls]


def test_httpx_fstring_becomes_a_template_with_the_host() -> None:
    src = '''
import httpx

def pay(i):
    return httpx.post(f"http://billing-service:8000/invoices/{i}/pay", json={})
'''
    assert _calls(PythonExtractor(), "a.py", src) == [
        ("POST", "billing-service", "/invoices/{}/pay", "pay")]


def test_a_client_with_base_url_keeps_its_host() -> None:
    src = '''
import httpx

async def go(i):
    async with httpx.AsyncClient(base_url="http://billing-service") as client:
        await client.get("/health")
        await client.request("DELETE", f"/invoices/{i}")
'''
    assert _calls(PythonExtractor(), "a.py", src) == [
        ("GET", "billing-service", "/health", "go"),
        ("DELETE", "billing-service", "/invoices/{}", "go")]


def test_requests_with_a_variable_base_gives_a_host_hint() -> None:
    src = '''
import requests

def f(i):
    requests.get(BILLING_URL + "/invoices/" + str(i))
    requests.put(f"{self.base_url}/x/{i}")
'''
    assert _calls(PythonExtractor(), "a.py", src) == [
        ("GET", "BILLING_URL", "/invoices/{}", "f"), ("PUT", "base_url", "/x/{}", "f")]


def test_aiohttp_session_and_a_client_named_call() -> None:
    src = '''
import aiohttp

async def f(i):
    async with aiohttp.ClientSession() as session:
        await session.post(f"http://billing-service/a/{i}")
    cfg.get("some-key")
'''
    assert _calls(PythonExtractor(), "a.py", src) == [("POST", "billing-service", "/a/{}", "f")]


def test_fetch_axios_and_an_axios_instance() -> None:
    src = '''
const API = "http://billing-service:8000";
const ax = axios.create({ baseURL: "http://orders-service/v1" });
async function f(id) {
  await fetch(`${API}/invoices/${id}/pay`, { method: "POST" });
  await fetch("/api/local");
  await ax.delete(`/orders/${id}`);
  await axios.post(process.env.PAYMENTS_URL + "/charge", {});
  await axios({ url: "/x", method: "put" });
}
'''
    assert _calls(TypeScriptExtractor(), "client.ts", src) == [
        ("POST", "billing-service", "/invoices/{}/pay", "f"),
        ("GET", "", "/api/local", "f"),
        ("DELETE", "orders-service", "/v1/orders/{}", "f"),
        ("POST", "PAYMENTS_URL", "/charge", "f"),
        ("PUT", "", "/x", "f")]


def test_go_http_calls_sprintf_and_getenv() -> None:
    src = '''package main
const Base = "http://billing-service:8000"
func pay(id string) {
  http.NewRequest(http.MethodPost, fmt.Sprintf("%s/invoices/%s/pay", Base, id), nil)
  http.Get(os.Getenv("ORDERS_URL") + "/orders")
  client.Post("http://billing-service/charge", "application/json", nil)
}
'''
    assert _calls(GoExtractor(), "pay.go", src) == [
        ("POST", "billing-service", "/invoices/{}/pay", "pay"),
        ("GET", "ORDERS_URL", "/orders", "pay"),
        ("POST", "billing-service", "/charge", "pay")]


def test_resttemplate_and_webclient() -> None:
    src = '''
class Pay {
  void a(long id) {
    restTemplate.postForObject("http://billing-service/invoices/" + id + "/pay", body, Res.class);
    restTemplate.exchange("http://billing-service/x", HttpMethod.PUT, entity, Res.class);
    webClient.delete().uri("/orders/{id}", id).retrieve();
    webClient.method(HttpMethod.PATCH).uri("/orders").retrieve();
    list.put(key, value);
  }
}
'''
    assert _calls(JavaExtractor(), "Pay.java", src) == [
        ("POST", "billing-service", "/invoices/{}/pay", "a"),
        ("PUT", "billing-service", "/x", "a"),
        ("DELETE", "", "/orders/{}", "a"),
        ("PATCH", "", "/orders", "a")]
