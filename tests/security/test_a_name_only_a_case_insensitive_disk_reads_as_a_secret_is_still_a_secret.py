"""A secret's name is judged the way the disk will read it, not the way it was typed.

THE DEFECT. Names were compared after `str.lower()`. APFS and NTFS compare names
with FULL case folding, so `ſecrets.json` (U+017F, long s) and `id_rſa` open
`secrets.json` and `id_rsa`, and a Kelvin sign (U+212A) is a `k`; `lower()` leaves
the long s alone, so every name rule (`secrets.json`, `id_rsa*`, `.ssh/`,
`credentials`, `private_keys/`, `*.tfstate`, `.htpasswd`, `.pgpass`) was bypassed
for every reader by one character. 9 of the 15 aliases that exist on a macOS disk
slipped through before the fix.

THE CONTRACT. `casefold()` plus compatibility normalisation (`NFKC`) everywhere a
name is compared: built-in globs, directories, operator globs.
"""

from __future__ import annotations

import os

import pytest

from src.security import secret_files as sf

# (canonical secret name, its alias)
ALIASES = [
    ("secrets.json", "ſecrets.json"),
    ("secrets.json", "secretſ.json"),
    ("id_rsa", "id_rſa"),
    ("credentials", "credentialſ"),
    (".ssh/k", ".ſsh/k"),
    ("private_keys/a.txt", "private_keyſ/a.txt"),
    ("secret/a.txt", "ſecret/a.txt"),
    ("terraform.tfstate", "terraform.tfſtate"),
    (".htpasswd", ".htpaſswd"),
    (".pgpass", ".pgpaſs"),
    (".pgpass", ".pgpaß"),  # full case folding: ß is ss
    (".htpasswd", ".htpaßwd"),
    ("server.pem", "ſerver.pem"),
    ("x.key", "x.Key"),
    ("kubeconfig", "Kubeconfig"),
    (".aws/credentials", ".awſ/credentials"),
    ("config/credentials.yml", "config/credentialſ.yml"),
    # the compatibility spellings (fullwidth, ligature) and the plain case variants
    (".env", "．env"),
    (".env", "．ｅｎｖ"),
    ("secrets.json", "SECRETS.JSON"),
    ("id_rsa", "ID_RSA"),
    ("terraform.tfstate", "terraform.tfﬆate"),
    (".git/config", ".GIT/config"),
]


@pytest.mark.parametrize(("canonical", "alias"), ALIASES)
def test_an_alias_of_a_secret_name_is_judged_like_the_name_itself(canonical, alias, tmp_path):
    assert sf.classify(canonical) == "deny", "the canonical name is a secret (test data)"

    assert sf.classify(alias) == "deny", ascii(alias)
    assert sf.is_secret_path(alias) is True
    root = tmp_path / "r"
    (root / os.path.dirname(alias)).mkdir(parents=True, exist_ok=True)
    (root / alias).write_text("x\n")
    assert sf.plain_file(root, alias) is None, "the reader that opens the file is refused"
    with pytest.raises(sf.SecretPathRefused):
        sf.safe_join(root, alias)


def test_an_operators_glob_is_case_folded_like_the_built_in_ones(monkeypatch):
    monkeypatch.setattr(sf, "_extra_globs", lambda: ("Deploy/Internal-*.txt",))

    assert sf.classify("deploy/internal-notes.txt") == "deny"
    assert sf.classify("DEPLOY/INTERNAL-NOTES.TXT") == "deny"
    assert sf.classify("deploy/ınternal-notes.txt") == "ok", "dotless i is not an i on any disk"


_CANON = ["secrets.json", "id_rsa", "credentials", ".env", ".npmrc", ".pgpass", ".htpasswd", "server.pem",
          "x.key", "terraform.tfstate", ".netrc", "kubeconfig", ".ssh/k", ".aws/credentials",
          "private_keys/a.txt", "secret/a.txt"]
_CANDIDATES = ["ſ", "K", "ß", "ı", "İ", "Ω", "Å", "ｓ", "ｋ", "ﬆ"]


def test_every_character_this_disk_treats_as_another_one_is_caught(tmp_path):
    """The brute force, fast subset: ask THIS filesystem which stand-in characters open the same
    file, then require the classifier to catch every alias it found. (On a case-sensitive disk
    nothing aliases and this passes on the explicit table above; on macOS it found 15.)"""
    root = tmp_path / "bf"
    for name in _CANON:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    found = missed = 0
    for name in _CANON:
        ino = os.stat(root / name).st_ino
        for i, ch in enumerate(name):
            if ch in "/.":
                continue
            for cand in _CANDIDATES:
                alias = name[:i] + cand + name[i + 1:]
                try:
                    same = os.stat(root / alias).st_ino == ino
                except (OSError, ValueError):
                    continue
                if not same:
                    continue
                found += 1
                if not sf.is_secret_path(alias) or sf.plain_file(root, alias) is not None:
                    missed += 1
    assert missed == 0, f"{missed} of {found} aliases of a secret name were not recognised"
