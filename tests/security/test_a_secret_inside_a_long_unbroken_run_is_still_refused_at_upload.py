"""A secret inside one unbroken run of 2048+ characters (minified JSON) is refused.

The contract (src/security/mcp_redact.py `scan_lines(window_long_runs=True)`,
src/knowledge/ingest.py, docs/CODEBASE.md "The upload scan"):

  * the upload scan reads a line that holds a long run in overlapping windows, so
    a password, a connection string, an API key or a client secret inside minified
    JSON is found by the same rule that finds it on a short line, wherever it sits
    in the run (start, middle, end, across a window boundary), and a name at the
    end of one run is read together with its value after the next blank;
  * the upload is refused with `secrets_found` naming file, line and rule, and
    nothing of the line — neither the secret nor the run around it;
  * a long run without a secret (a base64 image, minified JSON) is still accepted;
  * the OUTPUT side is unchanged: `redact_for_mcp` withholds the whole run, and
    `scan_lines` without the flag (the howto warnings) does not look inside it;
  * every rule reads at most one window at a time and the number of windows grows
    with the length of the run, so the cost is linear, and a maximal upload costs
    what the same characters cost as ordinary 2000-character lines.

Canaries are generated at runtime so no scanner fires on this file.
"""

from __future__ import annotations

import base64
import json
import math
import random
import secrets
import string
import time

import pytest

from src.knowledge import ingest
from src.security import mcp_redact
from src.security.mcp_redact import RUN_OVERLAP, RUN_WINDOW, redact_for_mcp, scan_lines

ALNUM = string.ascii_letters + string.digits
SKILL = "---\nname: demo\ndescription: demo skill\n---\n# demo\n"
STEP = RUN_WINDOW - RUN_OVERLAP


def _rand(n: int) -> str:
    while True:
        value = "".join(secrets.choice(ALNUM) for _ in range(n))
        if any(c.isdigit() for c in value) and any(c.isalpha() for c in value):
            return value


def _minified(size: int, seed: int = 3) -> str:
    """Records joined by commas, no whitespace anywhere: one run of ``size`` characters."""
    rnd = random.Random(seed)
    parts: list[str] = []
    total = 0
    while total < size:
        part = json.dumps({"id": rnd.randint(1, 10**6),
                           "n": "".join(rnd.choices(string.ascii_lowercase, k=8)),
                           "t": [1, 2], "ok": True}, separators=(",", ":"))
        parts.append(part)
        total += len(part) + 1
    return ",".join(parts)[:size]


def _fragments() -> dict[str, tuple[str, str]]:
    """name -> (fragment without whitespace, the secret in it)."""
    password, dsn_pass, key, client = _rand(18), _rand(14), _rand(24), _rand(30)
    return {
        "password": (f'{{"db":{{"user":"app","password":"{password}"}}}}', password),
        "dsn": (f'"url":"postgresql://app:{dsn_pass}@db.internal.example:5432/app"', dsn_pass),
        "api key": (f'{{"api_key":"{key}"}}', key),
        "client secret": (f'{{"oauth":{{"client_secret":"{client}"}}}}', client),
    }


def _run_with(fragment: str, at: int, size: int = 9000) -> str:
    body = _minified(size)
    line = body[:at] + "," + fragment + "," + body[at:]
    assert " " not in line and len(line) > mcp_redact.MAX_RUN
    return line


def _upload(body: str):
    return ingest.ingest_files([("demo/SKILL.md", (SKILL + body + "\n").encode())])


def _labels(line: str) -> list[tuple[int, str]]:
    return scan_lines(line, source_hint="demo/SKILL.md", window_long_runs=True)


# ─── found wherever it sits ──────────────────────────────────────────

@pytest.mark.parametrize("kind", list(_fragments()))
@pytest.mark.parametrize("at", [0, 700, STEP - 20, STEP, RUN_WINDOW - 30, 2 * STEP - 10, 5000, 8990])
def test_a_secret_inside_minified_json_is_found_by_the_rule_that_finds_it_on_a_short_line(kind, at):
    fragment, _secret = _fragments()[kind]
    expected = scan_lines(fragment, source_hint="demo/SKILL.md")
    assert len(expected) == 1, "the short form must be a secret to begin with"
    assert _labels(_run_with(fragment, at)) == [(1, expected[0][1])]


def test_a_secret_is_found_at_every_offset_across_three_windows():
    fragment, _secret = _fragments()["password"]
    base = _minified(3 * RUN_WINDOW)
    missed = [at for at in range(0, len(base), 53)
              if not _labels(base[:at] + "," + fragment + "," + base[at:])]
    assert missed == []


def test_a_secret_in_the_second_run_of_a_line_is_found():
    fragment, _secret = _fragments()["dsn"]
    line = _minified(3000, seed=1) + " " + _run_with(fragment, 2500)
    assert [n for n, _ in _labels(line)] == [1]


@pytest.mark.parametrize("value_in_a_run", [False, True])
@pytest.mark.parametrize("shape", ['password = {v}', '"password": "{v}"', "'password' : '{v}'"])
def test_a_name_at_the_end_of_a_run_is_read_together_with_its_value_after_the_blank(shape, value_in_a_run):
    secret = _rand(16)
    pair = shape.format(v=secret)
    name, value = pair.split(" ", 1)
    assert scan_lines("a," + pair, source_hint="demo/SKILL.md") != [], "the short form must be a secret to begin with"
    tail = _minified(3000, seed=8) if value_in_a_run else ""
    line = _minified(3000, seed=7) + "," + name + " " + value + tail
    assert [n for n, _ in _labels(line)] == [1]
    with pytest.raises(ingest.IngestError) as refused:
        _upload(line)
    assert refused.value.code == "secrets_found" and secret not in repr(refused.value.hits)


def test_the_line_number_is_the_line_of_the_run():
    fragment, _secret = _fragments()["password"]
    text = "first line\n" + _minified(3000, seed=2) + "\n" + _run_with(fragment, 4000) + "\nlast"
    assert [n for n, _ in _labels(text)] == [3]


# ─── refused at upload, saying nothing of the line ───────────────────

@pytest.mark.parametrize("kind", list(_fragments()))
def test_an_upload_with_a_secret_inside_a_long_run_is_refused_naming_rule_and_line_only(kind):
    fragment, secret = _fragments()[kind]
    line = _run_with(fragment, 4200)
    with pytest.raises(ingest.IngestError) as refused:
        _upload(line)
    err = refused.value
    label = scan_lines(fragment, source_hint="SKILL.md")[0][1]
    assert err.code == "secrets_found"
    assert err.hits == [{"path": "SKILL.md", "line": 6, "label": label}]
    shown = err.message + repr(err.hits) + str(err)
    assert secret not in shown and line[:200] not in shown and line[1000:1200] not in shown


# ─── what stays accepted, what stays as it was ───────────────────────

def test_a_long_run_without_a_secret_is_still_accepted():
    for body in (_minified(200_000),
                 "![x](data:image/png;base64," + base64.b64encode(random.Random(4).randbytes(60_000)).decode() + ")",
                 "a." * 50_000):
        assert _labels(body) == []
        assert _upload(body).files


def test_without_the_flag_a_run_is_still_not_read_and_the_output_still_withholds_it():
    fragment, secret = _fragments()["password"]
    line = _run_with(fragment, 4200)
    assert scan_lines(line, source_hint="demo/SKILL.md") == []
    out, stats = redact_for_mcp(line, source_hint="demo/data.json")
    assert "[REDACTED:long-literal]" in out and secret not in out and len(out) < 200
    assert "long-literal" in stats.patterns_matched


def test_a_secret_next_to_the_run_on_the_same_line_keeps_its_own_label():
    password = _rand(18)
    line = _minified(3000) + f" export DB_PASSWORD={password}"
    assert _labels(line) == [(1, "shell-env-secret")]


# ─── bounded and linear ──────────────────────────────────────────────

def test_every_rule_reads_one_window_at_a_time_and_the_windows_overlap(monkeypatch):
    seen: list[str] = []

    def spy(text, hits, ctx):
        seen.append(text)
        return text

    monkeypatch.setattr(mcp_redact, "_WINDOW_RULES", (spy,))
    run = _minified(1_000_000)
    assert _labels(run) == []
    assert max(map(len, seen)) <= RUN_WINDOW < mcp_redact.MAX_RUN
    assert len(seen) == math.ceil((len(run) - RUN_WINDOW) / STEP) + 1
    assert "".join(w[:STEP] for w in seen[:-1]) + seen[-1] == run
    assert all(a[-RUN_OVERLAP:] == b[:RUN_OVERLAP] for a, b in zip(seen, seen[1:], strict=False))


SHAPES = {
    "one long word": lambda n: "a" * n,
    "dotted run": lambda n: "a." * (n // 2),
    "dashed run": lambda n: "a-" * (n // 2),
    "equals run": lambda n: "a=" * (n // 2),
    "quoted pairs": lambda n: '"a":"' * (n // 5),
    "unterminated shell default": lambda n: "${a:-" * (n // 5),
    "scheme dots": lambda n: "a.b+c-" * (n // 6) + "://",
    "many secret names": lambda n: '{"password":' * (n // 13),
    "base64 blob": lambda n: "QUJD" * (n // 4),
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_a_hostile_long_run_is_scanned_fast(shape):
    text = SHAPES[shape](200_000)
    t0 = time.monotonic()
    _labels(text)
    assert time.monotonic() - t0 < 3.0


def test_a_maximal_upload_costs_what_the_same_text_costs_as_ordinary_lines():
    """Sixteen files of 512 KB, each ONE run, against a sample of the same text
    cut into 2000-character lines (what a run would cost if it were not a run)."""
    cap = ingest.MAX_FILE_BYTES
    files = [(f"demo/data{i}.md", _minified(cap, seed=i)) for i in range(ingest.MAX_TOTAL_BYTES // cap)]
    assert sum(len(t) for _, t in files) == ingest.MAX_TOTAL_BYTES

    sample = _minified(cap, seed=99)
    cut = "\n".join(sample[i:i + 2000] for i in range(0, len(sample), 2000))
    t0 = time.perf_counter()
    scan_lines(cut, source_hint="demo/data.md", window_long_runs=True)
    per_file_as_lines = time.perf_counter() - t0

    t0 = time.perf_counter()
    ingest._refuse_secrets(files)
    whole = time.perf_counter() - t0
    assert whole < 3 * per_file_as_lines * len(files) + 1.0
