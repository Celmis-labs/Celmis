"""`celmis-pack` verifies https even on a Python that ships without root
certificates, and never by turning verification off.

The failure (python.org's macOS build, until its "Install Certificates.command"
has run): `ssl.get_default_verify_paths()` has no cafile, the openssl_cafile path is
missing, the store is empty, and every https request fails with
CERTIFICATE_VERIFY_FAILED "unable to get local issuer certificate". The tool then
said only "the server's TLS certificate could not be verified".

The contract (bin/celmis-pack `tls_context`):

  * with no certificates loaded AND neither SSL_CERT_FILE nor SSL_CERT_DIR set, the
    first system bundle that exists and loads is added (macOS's /etc/ssl/cert.pem
    first, then the usual Linux paths);
  * with certificates already loaded, or either variable set, nothing is added: the
    person or the Python build chose, and the tool does not second-guess it;
  * in every case the context still requires a valid certificate for the host
    name, and the tool has no switch (and no line of code) that relaxes it;
  * when verification fails the message gives the reason OpenSSL gave and names
    the two fixes: the "Install Certificates.command" that came with Python, or
    SSL_CERT_FILE pointing at a CA bundle.
"""

from __future__ import annotations

import datetime
import ipaddress
import re
import ssl
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.plugin.test_the_celmis_pack_tool_publishes_checks_and_fetches_a_pack import (
    CLI,
    GOOD,
    cli,
    run,
    status_json,
)

HOST = "127.0.0.1"


def make_cert(name: str) -> tuple[bytes, bytes]:
    """(certificate PEM, private key PEM): a self-signed CA certificate for HOST."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(HOST))]),
                           critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


@pytest.fixture
def no_certificates(monkeypatch):
    """A Python with an empty root store, whatever the machine running the test has."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(cli.ssl, "create_default_context", lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))


@pytest.fixture
def bundles(tmp_path, monkeypatch):
    """Four system bundle paths: missing, not a certificate file, one certificate,
    two certificates. What a context holds afterwards says which one was loaded."""
    one, two = make_cert("one")[0], make_cert("two")[0]
    paths = {"missing": tmp_path / "missing.pem", "broken": tmp_path / "broken.pem",
             "one": tmp_path / "one.pem", "two": tmp_path / "two.pem"}
    paths["broken"].write_text("this is not a certificate\n")
    paths["one"].write_bytes(one)
    paths["two"].write_bytes(two + make_cert("three")[0])
    monkeypatch.setattr(cli, "_CA_BUNDLES", tuple(str(p) for p in paths.values()))
    return paths


def roots(ctx: ssl.SSLContext) -> int:
    return ctx.cert_store_stats()["x509_ca"]


def still_verifies(ctx: ssl.SSLContext) -> bool:
    return ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_with_no_certificates_and_no_variable_the_first_system_bundle_that_loads_is_added(
        no_certificates, bundles):
    ctx = cli.tls_context()
    assert roots(ctx) == 1                    # "one": the missing and the broken file were passed over
    assert still_verifies(ctx)


def test_nothing_is_added_when_the_python_already_has_certificates(monkeypatch, bundles):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    def own_roots():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=str(bundles["two"]))
        return ctx

    monkeypatch.setattr(cli.ssl, "create_default_context", own_roots)
    ctx = cli.tls_context()
    assert roots(ctx) == 2                    # its own two, not "one" on top
    assert still_verifies(ctx)


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_nothing_is_added_when_the_person_named_a_bundle_or_a_folder(
        no_certificates, bundles, monkeypatch, variable):
    monkeypatch.setenv(variable, str(bundles["one"]))
    ctx = cli.tls_context()
    assert roots(ctx) == 0
    assert still_verifies(ctx)


def test_when_no_system_bundle_exists_the_context_stays_empty_and_still_verifies(
        no_certificates, bundles, monkeypatch):
    monkeypatch.setattr(cli, "_CA_BUNDLES", (str(bundles["missing"]), str(bundles["broken"])))
    ctx = cli.tls_context()
    assert roots(ctx) == 0
    assert still_verifies(ctx)


def test_the_opener_the_tool_uses_verifies_certificates_and_host_names():
    https = next(h for h in cli._OPENER.handlers if isinstance(h, urllib.request.HTTPSHandler))
    assert still_verifies(https._context)


def test_the_tool_has_no_way_to_turn_verification_off():
    text = CLI.read_text(encoding="utf-8")
    for relaxed in ("CERT_NONE", "CERT_OPTIONAL", "_create_unverified_context", "_create_default_https_context",
                    "--insecure", "--no-verify", "--cacert"):
        assert relaxed not in text, relaxed
    assert not re.search(r"check_hostname\s*=\s*False", text)
    assert not re.search(r"verify_mode\s*=", text)


# ─── a real handshake ────────────────────────────────────────────────


class TlsServer:
    """A Celmis status answer behind a certificate nobody but the test trusts."""

    def __init__(self, tmp_path):
        cert, key = make_cert("celmis test server")
        self.cert = tmp_path / "server-cert.pem"
        self.cert.write_bytes(cert)
        keyfile = tmp_path / "server-key.pem"
        keyfile.write_bytes(key)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = status_json({})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer((HOST, 0), Handler)
        self.server.handle_error = lambda *a: None             # a refused handshake is the point
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self.cert, keyfile)
        self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://{HOST}:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def tls_server(tmp_path):
    server = TlsServer(tmp_path)
    try:
        yield server
    finally:
        server.close()


def test_an_untrusted_certificate_is_refused_and_the_message_says_how_to_fix_a_missing_bundle(tls_server):
    code, out, err = run("status", url=tls_server.url, token=GOOD)
    assert (code, out) == (1, "")
    assert "TLS certificate could not be verified" in err
    assert "Install Certificates.command" in err and "SSL_CERT_FILE" in err
    assert GOOD not in err


def test_the_bundle_the_person_names_in_ssl_cert_file_is_what_the_tool_trusts(tls_server):
    code, out, err = run("status", url=tls_server.url, token=GOOD, extra={"SSL_CERT_FILE": str(tls_server.cert)})
    assert code == 0, (out, err)
    assert "pack: ledger" in out


def test_a_certificate_for_another_host_is_refused_without_the_root_certificates_advice(tls_server):
    # Trusted root, wrong name: the bundle is fine, so advice about it would mislead.
    url = tls_server.url.replace(HOST, "localhost")
    code, out, err = run("status", url=url, token=GOOD, extra={"SSL_CERT_FILE": str(tls_server.cert)})
    assert (code, out) == (1, "")
    assert "TLS certificate could not be verified" in err
    assert "Install Certificates.command" not in err
    assert GOOD not in err
