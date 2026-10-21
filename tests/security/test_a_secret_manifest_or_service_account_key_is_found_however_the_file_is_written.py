"""A Kubernetes `Secret` or a service-account key is found whatever the layout, the line ends and the encoding.

THE DEFECT. The content sniff for `kind: Secret` had grown strict (`[ \\t]*` after the colon, the key at the very
start of the line): a manifest saved on Windows (CRLF), on classic Mac (a lone CR), with a byte order mark, as UTF-16,
indented (an item of a `List`, the `kubectl get -o yaml` shape, a list entry), with a quoted key or value, in YAML
flow style (`{kind: Secret}`) or as `type: service_account` in YAML was read like any file. The value of a Secret is
only base64: whoever reads the file has the password.

THE CONTRACT. A data file (not source code, prose or media) is refused by `classify_file`, `plain_file`, `safe_join`
and the code reader when it holds a `Secret` or a service-account key in ANY of those spellings; UTF-16 and UTF-32
text, with or without a BOM, is read as the ASCII keywords it spells. Code and prose that merely MENTION the words
(`kind = "Secret"`, a TypeScript `kind: "Secret";`, `kind: SecretStore`, a YAML comment) stay readable; the strict
shapes that were always refused (a bare `kind: Secret` at the start of a line, the JSON `"kind": "Secret"`) are refused
in every file, as before.
"""

from __future__ import annotations

import pytest

from src.security import secret_files as sf

PW = "CANARY_SECRET_c41e"
TAIL = f"data:\n  password: {PW}\n"


def _enc(text: str, how: str) -> bytes:
    return {
        "utf-8": text.encode(),
        "utf-8-bom": b"\xef\xbb\xbf" + text.encode(),
        "crlf": text.replace("\n", "\r\n").encode(),
        "cr": text.replace("\n", "\r").encode(),
        "u2028": text.replace("\n", "\u2028").encode(),
        "formfeed": text.replace("\n", "\x0c").encode(),
        "utf-16": text.encode("utf-16"),
        "utf-16-le": text.encode("utf-16-le"),
        "utf-16-be": text.encode("utf-16-be"),
        "utf-16-le-crlf": text.replace("\n", "\r\n").encode("utf-16-le"),
        "utf-32": text.encode("utf-32"),
    }[how]


MANIFESTS = {
    "plain": f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\n{TAIL}",
    "first-line": f"kind: Secret\n{TAIL}",
    "trailing-comment": f"kind: Secret   # prod\n{TAIL}",
    "trailing-spaces": f"kind: Secret \n{TAIL}",
    "tab-after-colon": f"kind:\tSecret\n{TAIL}",
    "double-quoted": f'kind: "Secret"\n{TAIL}',
    "single-quoted": f"kind: 'Secret'\n{TAIL}",
    "quoted-key": f'apiVersion: v1\n"kind": Secret\n{TAIL}',
    "single-quoted-key": f"apiVersion: v1\n'kind': 'Secret'\n{TAIL}",
    "tag": f"kind: !!str Secret\n{TAIL}",
    "value-on-the-next-line": f"kind:\n  Secret\n{TAIL}",
    "list-item": f"items:\n- apiVersion: v1\n  kind: Secret\n  {TAIL.replace(chr(10), chr(10) + '  ').rstrip()}\n",
    "kubectl-get-shape": (
        "apiVersion: v1\nitems:\n- apiVersion: v1\n  data:\n    password: " + PW +
        "\n  kind: Secret\n  metadata:\n    name: db\nkind: List\n"),
    "dash-list": f"- kind: Secret\n  {TAIL.strip().replace(chr(10), chr(10) + '  ')}\n",
    "indented-under-a-document": f"spec:\n    template:\n        kind: Secret\n        {TAIL.strip()}\n",
    "flow-mapping": "{apiVersion: v1, kind: Secret, data: {password: " + PW + "}}\n",
    "flow-first": "{kind: Secret, data: {password: " + PW + "}}\n",
    "json": '{"apiVersion": "v1", "kind": "Secret", "data": {"password": "' + PW + '"}}\n',
    "json-pretty": '{\n  "kind": "Secret",\n  "data": {"password": "' + PW + '"}\n}\n',
    "json-minified-in-a-list": '{"items":[{"kind":"Secret","data":{"p":"' + PW + '"}}]}\n',
    "json-key-and-colon-apart": '{"kind"\n:\n"Secret", "data": {"p": "' + PW + '"}}\n',
    "document-after-others": "a: 1\n---\n# a comment\n---\n" + f"kind: Secret\n{TAIL}",
}
SERVICE_ACCOUNTS = {
    "json": '{"type": "service_account", "private_key_id": "' + PW + '"}\n',
    "json-minified": '{"type":"service_account","private_key_id":"' + PW + '"}\n',
    "json-split": '{"type"\n:\n"service_account", "private_key_id": "' + PW + '"}\n',
    "yaml": f"type: service_account\nprivate_key_id: {PW}\n",
    "yaml-quoted": f'type: "service_account"\nprivate_key_id: {PW}\n',
    "yaml-quoted-key": f"'type': service_account\nprivate_key_id: {PW}\n",
    "yaml-indented": f"credentials:\n  gcp:\n    type: service_account\n    private_key_id: {PW}\n",
    "yaml-flow": "{type: service_account, private_key_id: " + PW + "}\n",
}
WIRE = {"utf-8", "utf-8-bom", "crlf", "cr", "u2028", "formfeed", "utf-16", "utf-16-le", "utf-16-be",
        "utf-16-le-crlf", "utf-32"}


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "acme-app"
    root.mkdir()
    return root


def _put(root, rel: str, data: bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _refused_everywhere(root, rel: str) -> None:
    from src.retrieval.tier3_code import CodeReader

    assert sf.classify_file(root, rel) == "deny", rel
    assert sf.content_verdict(root, rel) == "deny", rel
    assert sf.plain_file(root, rel) is None, rel
    with pytest.raises(sf.SecretPathRefused):
        sf.safe_join(root, rel)
    assert CodeReader().read_full_file(root, rel) is None, rel


@pytest.mark.parametrize("how", ["utf-8", "crlf", "cr", "utf-8-bom", "utf-16"])
@pytest.mark.parametrize("shape", sorted(MANIFESTS))
def test_a_secret_manifest_is_refused_in_every_layout_and_every_line_ending(repo, shape, how):
    _put(repo, "deploy/db.yaml", _enc(MANIFESTS[shape], how))

    _refused_everywhere(repo, "deploy/db.yaml")


@pytest.mark.parametrize("how", sorted(WIRE))
def test_a_secret_manifest_is_refused_in_every_line_ending_and_encoding(repo, how):
    _put(repo, "deploy/db.yaml", _enc(MANIFESTS["plain"], how))

    _refused_everywhere(repo, "deploy/db.yaml")


@pytest.mark.parametrize("how", ["utf-8", "crlf", "cr", "utf-8-bom", "utf-16", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("shape", sorted(SERVICE_ACCOUNTS))
def test_a_service_account_key_is_refused_in_json_and_in_yaml_in_any_encoding(repo, shape, how):
    _put(repo, "gcp/key-notes", _enc(SERVICE_ACCOUNTS[shape], how))

    _refused_everywhere(repo, "gcp/key-notes")


@pytest.mark.parametrize("ext", ["yaml", "yml", "json", "txt", "tpl", "sh", "j2", "cfg", "unknownext", ""])
def test_the_layout_is_judged_whatever_the_file_is_called(repo, ext):
    _put(repo, f"deploy/bundle.{ext}" if ext else "deploy/bundle", _enc(MANIFESTS["kubectl-get-shape"], "crlf"))

    _refused_everywhere(repo, f"deploy/bundle.{ext}" if ext else "deploy/bundle")


def test_a_manifest_after_a_long_run_of_other_documents_is_found_in_utf_16_too(repo):
    pad = "# " + "x" * 80 + "\n"
    _put(repo, "deploy/all.yaml", ("a: 1\n" + pad * 4000 + "---\n" + MANIFESTS["plain"]).encode("utf-16-le"))

    _refused_everywhere(repo, "deploy/all.yaml")


# --- what only MENTIONS the words ------------------------------------------------------------------------------

READABLE_DATA = {
    "kind-secretstore": "apiVersion: external-secrets.io/v1\nkind: SecretStore\nspec: {}\n",
    "kind-externalsecret": "kind: ExternalSecret\nspec:\n  secretStoreRef:\n    kind: SecretStore\n",
    "kind-secretproviderclass": "kind: SecretProviderClass\n",
    "kind-sealedsecret": "kind: SealedSecret\nspec:\n  encryptedData: {}\n",
    "kind-secret-dot-name": "kind: Secret.v1\n",
    "kind-secret-and-words": "notes: the kind: Secret santa list\n",
    "commented-out": "# kind: Secret\napiVersion: v1\nkind: ConfigMap\n",
    "mid-line-in-a-sentence": "description: use kind: Secret for passwords, not a ConfigMap\n",
    "mid-line-in-a-list-of-fields": "fields: name: db, kind: Secret.\n",
    "type-service-accounts": "type: service_accounts\nname: x\n",
    "type-service-account-key": "type: service_account_key\n",
    "type-in-prose": "description: a type: service_account pair\n",
    "configmap": "apiVersion: v1\nkind: ConfigMap\ndata:\n  kind: Secretive\n",
    "other-type": '{"type": "external_account", "audience": "x"}\n',
}


@pytest.mark.parametrize("name", sorted(READABLE_DATA))
@pytest.mark.parametrize("how", ["utf-8", "crlf", "cr", "utf-16"])
def test_data_that_only_mentions_the_words_stays_readable(repo, name, how):
    from src.retrieval.tier3_code import CodeReader

    _put(repo, "deploy/x.yaml", _enc(READABLE_DATA[name], how))

    assert sf.classify_file(repo, "deploy/x.yaml") == "ok"
    assert sf.plain_file(repo, "deploy/x.yaml") is not None
    assert CodeReader().read_full_file(repo, "deploy/x.yaml") is not None


READABLE_CODE = {
    "src/model.py": 'kind = "Secret"\nif obj.kind == "Secret":\n    pass\nkind: str = "Secret"\n',
    "src/node.ts": 'export interface SecretNode {\n  kind: "Secret";\n  name: string;\n}\n',
    "src/obj.ts": 'const a = { kind: "Secret", name: "x" };\nconst b = {\n  kind: "Secret",\n};\n',
    "src/kinds.go": 'package kinds\n\nvar x = meta{Kind: "Secret", Name: "x"}\n',
    "src/table.js": "const t = {\n    kind: 'Secret',\n    type: 'service_account',\n};\n",
    "src/Kind.java": 'class Kind {\n    String kind = "Secret";\n    String type = "service_account";\n}\n',
    "docs/secrets.md": "Set `kind: Secret` in the manifest, or use `type: service_account`.\n\n- kind: Secret holds a password\n",
    "docs/api.rst": "The ``kind`` field is ``Secret``;\n  kind: Secret  is shown indented in the example.\n",
}


@pytest.mark.parametrize("rel", sorted(READABLE_CODE))
def test_source_code_and_prose_that_merely_mention_the_words_stay_readable(repo, rel):
    _put(repo, rel, READABLE_CODE[rel].encode())
    head = READABLE_CODE[rel].encode()

    assert sf.classify_file(repo, rel) == "ok"
    assert sf.classify(rel, head) == "ok", "the upload-time sniff over the whole file reads source and prose strictly"
    assert sf.plain_file(repo, rel) is not None


def test_the_strict_shapes_are_still_refused_in_source_and_prose_when_the_caller_hands_over_the_content():
    manifest = b"apiVersion: v1\nkind: Secret\ndata: {}\n"
    sa = b'{"type": "service_account", "private_key_id": "x"}\n'
    pem = b"-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"

    for rel in ("docs/example.md", "src/fixture.py", "web/sample.ts", "k/x.yaml"):
        assert sf.classify(rel, manifest) == "deny", rel
        assert sf.classify(rel, sa) == "deny", rel
        assert sf.classify(rel, pem) == "deny", rel
    for rel in ("docs/example.md", "src/fixture.py", "k/x.yaml"):
        assert sf.classify(rel, b"kind: Secret   # prod\n") == "deny", rel
        assert sf.classify(rel, b"kind: Secret  \r\ndata: {}\r\n") == "deny", rel
        assert sf.classify(rel, b"kind: 'Secret'\n") == "deny", rel
    assert sf.classify("k/x.yaml", b"  kind: Secret\n") == "deny"
    assert sf.classify("web/sample.ts", b"  kind: Secret\n") == "ok"


def test_a_crlf_manifest_is_the_regression_that_started_it(repo):
    """HEAD matched `kind:\\s*Secret\\s*$`; a version with `[ \\t]*` let `kind: Secret\\r\\n` through."""
    _put(repo, "k.yaml", b"apiVersion: v1\r\nkind: Secret\r\ndata:\r\n  p: eA==\r\n")

    assert sf.classify_file(repo, "k.yaml") == "deny"


def test_a_two_megabyte_line_of_braces_and_commas_is_sniffed_in_linear_time(repo):
    import time

    body = ("{,[ ]}\t" * 300_000)[: sf.SNIFF_MAX_BYTES - 100] + "\n"
    _put(repo, "deploy/noise.yaml", ("Secret " + body).encode())
    t0 = time.perf_counter()
    verdict = sf.classify_file(repo, "deploy/noise.yaml")
    elapsed = time.perf_counter() - t0

    assert verdict == "ok"
    assert elapsed < 5, f"{elapsed:.1f}s"
