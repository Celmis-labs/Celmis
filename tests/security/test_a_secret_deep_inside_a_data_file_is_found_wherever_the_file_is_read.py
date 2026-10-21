"""A data file whose CONTENT is a secret is withheld however far in the secret sits, whatever the file is called.

THE DEFECT. The content sniff read the first 8 KiB of a file, and only for a fixed list
of extensions. A multi-document `all-in-one.yaml` whose `kind: Secret` document comes
after 8 KiB of other manifests, a private key pasted at the end of `notes.txt`, a PEM in
`data/key.dat` or `backup.bak` were read like any file. The plain-text search and the
ranking behind `ask` (`text_search.iter_text_files`) did not sniff at all, so a PEM in
`data/looks_innocent.txt` was searched and its lines came back as hits.

THE CONTRACT.
  * the whole file is read, up to `SNIFF_MAX_BYTES`; a data file above that cannot be
    inspected and is withheld ("deny if unsure"), a lockfile excepted;
  * every extension that is not source code, prose, binary media or a lockfile is read
    (`.dat`, `.bak`, `.log`, `.cfg`, no extension, an unknown one);
  * the plain-text search asks the same question;
  * a second walk of an unchanged tree reads nothing again (cache by path, size, times).
"""

from __future__ import annotations

import pytest

from src.security import secret_files as sf

PAD = "# " + "x" * 80 + "\n"  # 83 bytes
SECRET = "CANARY_SECRET_a91c"
PEM = f"-----BEGIN RSA PRIVATE KEY-----\n{SECRET}\n-----END RSA PRIVATE KEY-----\n"
K8S = f"apiVersion: v1\nkind: Secret\ndata:\n  password: {SECRET}\n"


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "acme-app"
    root.mkdir()
    return root


def _put(root, rel: str, text: str | bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode())


DEEP = [
    ("deploy/all-in-one.yaml", "a: 1\n" + PAD * 110 + "---\n" + K8S),                  # ~9 KiB in
    ("deploy/big-bundle.yml", "a: 1\n" + PAD * 4000 + "---\n" + K8S),                  # ~330 KiB in
    ("notes/key_backup.txt", PAD * 120 + PEM),
    ("data/key.dat", PEM),
    ("data/backup.bak", PAD * 200 + PEM),
    ("data/server.log", PAD * 200 + PEM),
    ("conf/app.cfg", PAD * 200 + PEM),
    ("data/blob.unknownext", PAD * 200 + PEM),
    ("data/NOEXT", PAD * 200 + PEM),
    ("deploy/manifest.json", '{"items": []}\n' + PAD * 200 + '{"kind": "Secret", "data": {}}\n'),
    ("gcp/sa-notes.json", PAD * 200 + '{"type": "service_account", "private_key_id": "x"}\n'),
    ("deploy/secret.sh", "#!/bin/sh\n" + PAD * 200 + "cat > k <<EOF\n" + PEM + "EOF\n"),
]


@pytest.mark.parametrize(("rel", "text"), DEEP, ids=[d[0] for d in DEEP])
def test_a_secret_anywhere_in_a_data_file_withholds_the_file_from_every_reader(repo, rel, text):
    from src.retrieval.tier3_code import CodeReader

    _put(repo, rel, text)

    assert sf.classify_file(repo, rel) == "deny"
    assert sf.content_verdict(repo, rel) == "deny"
    assert sf.plain_file(repo, rel) is None
    with pytest.raises(sf.SecretPathRefused):
        sf.safe_join(repo, rel)
    assert CodeReader().read_full_file(repo, rel) is None


def test_a_secret_in_a_utf16_file_is_found_too(repo):
    _put(repo, "notes/win.txt", ("x\n" * 5000 + PEM).encode("utf-16"))

    assert sf.classify_file(repo, "notes/win.txt") == "deny"


@pytest.mark.parametrize(("rel", "text"), [
    ("src/keys.py", PAD * 200 + "TEST_KEY = '''" + PEM + "'''\n"),
    ("docs/keys.md", PAD * 200 + "```\n" + PEM + "```\n"),
    ("src/Seeds.java", "class Seeds { String k = \"-----BEGIN RSA PRIVATE KEY-----\"; }\n"),
    ("deploy/config-map.yaml", "kind: ConfigMap\ndata:\n  a: b\n" + PAD * 200),
    ("deploy/secret-handling.yaml", "# kind: Secret is handled elsewhere\nkind: Deployment\n"),
], ids=["python source", "markdown", "java source", "a ConfigMap", "a comment"])
def test_source_prose_and_other_kinds_are_not_hidden_for_what_they_mention(repo, rel, text):
    """Source code and prose are not sniffed (a secret literal in them is the redactor's
    business); a YAML that merely MENTIONS the word is not a Secret."""
    _put(repo, rel, text)

    assert sf.classify_file(repo, rel) == "ok"
    assert sf.plain_file(repo, rel) is not None


def test_a_data_file_too_large_to_inspect_is_withheld_and_a_lockfile_is_not(repo):
    big = b"a,b\n" * (sf.SNIFF_MAX_BYTES // 4 + 10)
    _put(repo, "data/export.csv", big)
    _put(repo, "package-lock.json", b'{"lockfileVersion": 3}\n' + b" " * (sf.SNIFF_MAX_BYTES + 10))
    _put(repo, "data/small.csv", b"a,b\n1,2\n")
    _put(repo, "web/dist/app.min.js", b"x" * (sf.SNIFF_MAX_BYTES + 10))

    assert sf.classify_file(repo, "data/export.csv") == "deny", "cannot be looked through: withheld"
    assert sf.plain_file(repo, "data/export.csv") is None
    assert sf.classify_file(repo, "package-lock.json") == "ok"
    assert sf.classify_file(repo, "data/small.csv") == "ok"
    assert sf.classify_file(repo, "web/dist/app.min.js") == "ok", "source is never read for the sniff"


def test_an_unchanged_tree_is_not_read_again_and_a_changed_file_is(repo):
    _put(repo, "data/a.yaml", "a: 1\n")
    sf._sniff_file.cache_clear()

    assert sf.content_verdict(repo, "data/a.yaml") == "ok"
    assert sf.content_verdict(repo, "data/a.yaml") == "ok"
    info = sf._sniff_file.cache_info()
    assert (info.misses, info.hits) == (1, 1), "the second look cost a stat, not a read"

    _put(repo, "data/a.yaml", K8S)  # same path, new content
    assert sf.content_verdict(repo, "data/a.yaml") == "deny"


# ─── the plain-text search ───────────────────────────────────────────


def test_the_plain_text_search_never_searches_a_file_whose_content_is_a_secret(repo):
    from src.qa import text_search

    _put(repo, "data/looks_innocent.txt", PEM)
    _put(repo, "data/innocent.yaml", K8S)
    _put(repo, "deploy/all-in-one.yaml", "a: 1\n" + PAD * 110 + "---\n" + K8S)
    _put(repo, "data/export.csv", b"a,b\n" * (sf.SNIFF_MAX_BYTES // 4 + 10))
    _put(repo, "src/orders.py", f"def place_order():\n    return 1  # {SECRET}\n")

    walked = {rel for rel, _ in text_search.iter_text_files(repo)}
    hits, _complete = text_search.search_text(repo, SECRET)
    ranked = text_search.rank_files(repo, f"{SECRET} password")

    assert walked == {"src/orders.py"}
    assert {h.path for h in hits} == {"src/orders.py"}
    assert ranked == ["src/orders.py"]


# ─── howto's source tracer ───────────────────────────────────────────

_DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n"
    "        - name: web\n          env:\n            - name: DB_PASS\n              valueFrom:\n"
    "                secretKeyRef:\n                  name: db\n                  key: pass\n"
)


def test_the_howto_tracer_looks_through_the_whole_manifest_not_its_first_8_kib(repo):
    from src.mcp_server.howto import tracer

    _put(repo, "deploy/plain.yaml", _DEPLOYMENT + PAD * 110)
    _put(repo, "deploy/all-in-one.yaml", _DEPLOYMENT + PAD * 110 + "---\n" + K8S)

    found = {"DB_PASS": tracer.NameInfo("DB_PASS", tracer.classify_name("DB_PASS"))}
    tracer.trace_sources(found, repo, ["deploy/plain.yaml"])
    assert [s.split(":")[0] for s in found["DB_PASS"].sources] == ["deploy/plain.yaml"], "control: a plain manifest is traced"

    found = {"DB_PASS": tracer.NameInfo("DB_PASS", tracer.classify_name("DB_PASS"))}
    tracer.trace_sources(found, repo, ["deploy/all-in-one.yaml"])
    assert found["DB_PASS"].sources == [], "a manifest that carries a Secret document, deep or not, is not read"
    assert SECRET not in repr(found)
