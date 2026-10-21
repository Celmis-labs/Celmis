"""An example env file shows no value through a comment, and ends a line wherever a reader ends it.

THE DEFECT. `mask_env_values` kept every comment line as it was and kept a placeholder line whole, so
`# OLD_API_KEY=sk-live-...` (the key somebody commented out when they rotated it), `KEY=changeme  # was sk-live-...`
and a value after a form feed, a lone CR or U+2028 (`KEY=changeme<U+2028>SECRET=hunter2`, which an editor shows as two
lines and the masker saw as one placeholder) all came out in clear.

THE CONTRACT. A commented-out assignment is an assignment: its value is withheld unless it is empty or a placeholder,
and so is what the value goes on to over the comment lines that follow (a commented PEM block). A comment after a
placeholder value is kept when it is plain words (`PORT=8080  # default port`) and replaced by
`# [comment withheld]` when it could hold a token. A comment line without a `KEY=` (a heading, a `#!`, a note) is kept.
A line ends at any line terminator `str.splitlines` knows.
"""

from __future__ import annotations

import pytest

from src.security.secret_files import mask_env_values as mask

C = "CANARYVALUE9z"


@pytest.mark.parametrize(("text", "expected"), [
    # a commented-out assignment
    (f"# KEY={C}\n", "# KEY=[value withheld]"),
    (f"#KEY={C}\n", "#KEY=[value withheld]"),
    (f"## KEY={C}\n", "## KEY=[value withheld]"),
    (f"   #   KEY = {C}\n", "   #   KEY = [value withheld]"),
    (f"# export KEY={C}\n", "# export KEY=[value withheld]"),
    (f"# Set API_KEY={C} to enable it\n", "# Set API_KEY=[value withheld]"),
    (f"# old: DB_URL=postgres://u:{C}@db.internal/x\n", "# old: DB_URL=[value withheld]"),
    (f"# DB_PASSWORD: {C}\n", "# DB_PASSWORD: [value withheld]"),
    (f"# api_token: {C}\n", "# api_token: [value withheld]"),
    (f"# KEY=changeme # was {C}9\n", "# KEY=changeme # [comment withheld]"),
    # ... that is empty or a placeholder stays
    ("# KEY=\n", "# KEY="),
    ("# KEY=changeme\n", "# KEY=changeme"),
    ("# KEY=<your-key-here>\n", "# KEY=<your-key-here>"),
    ("# KEY=${OTHER}\n", "# KEY=${OTHER}"),
    ("# PORT=8080\n", "# PORT=8080"),
    # a comment that is not an assignment stays
    ("# Database settings\n", "# Database settings"),
    ("#!/bin/sh\n", "#!/bin/sh"),
    ("# NOTE: set it before the first run\n", "# NOTE: set it before the first run"),
    ("# Example: see the docs\n", "# Example: see the docs"),
    ("# see https://docs.example.com/page?a=b&c=d for details\n", "# see https://docs.example.com/page?a=b&c=d for details"),
    ("# ==== Section ====\n", "# ==== Section ===="),
    ("#\n", "#"),
    ("\n", ""),
    # a comment after a placeholder: plain words stay, anything that could be a token goes
    ("PORT=8080  # default port\n", "PORT=8080  # default port"),
    ("DEBUG=false # set to true to log more.\n", "DEBUG=false # set to true to log more."),
    ("MODE=changeme # one of: dev, prod\n", "MODE=changeme # one of: dev, prod"),
    (f"KEY=changeme # old sk-live-{C}\n", "KEY=changeme # [comment withheld]"),
    (f"KEY=changeme\t# old {C}\n", "KEY=changeme\t# [comment withheld]"),
    (f"KEY=changeme  #{C}\n", "KEY=changeme  # [comment withheld]"),
    (f"KEY=<your-key> # prod: {C}\n", "KEY=<your-key> # [comment withheld]"),
    ("KEY=changeme # see https://example.com/x\n", "KEY=changeme # [comment withheld]"),
    ("KEY=changeme # pass=hunter\n", "KEY=changeme # [comment withheld]"),
    (f"KEY=changeme # {'word ' * 40}\n", "KEY=changeme # [comment withheld]"),
    ("KEY=\n", "KEY="),
    # a value that is not a placeholder is still withheld, with or without a comment
    (f"KEY={C} # note\n", "KEY=[value withheld]"),
])
def test_a_commented_value_and_a_comment_after_a_placeholder_are_masked(text, expected):
    out = mask(text)

    assert out == expected
    assert C not in out


@pytest.mark.parametrize("terminator", ["\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "])
def test_a_line_ends_wherever_a_reader_ends_it(terminator):
    text = f"A=changeme{terminator}B={C}{terminator}# C={C}{terminator}D=changeme # {C}{terminator}"

    out = mask(text)

    assert C not in out
    assert out.split("\n") == ["A=changeme", "B=[value withheld]", "# C=[value withheld]",
                               "D=changeme # [comment withheld]"]


def test_a_lone_cr_file_masks_every_line_and_keeps_the_line_count():
    text = f"# header\rKEY={C}\rOTHER=changeme\rLAST={C}\r"

    assert mask(text) == "# header\nKEY=[value withheld]\nOTHER=changeme\nLAST=[value withheld]"


def test_a_commented_pem_block_is_withheld_to_its_last_line_and_the_next_assignment_is_read_again():
    text = (f"# SSH_KEY=-----BEGIN PRIVATE KEY-----\n# {C}AAAA\n#   {C}BBBB\n# -----END PRIVATE KEY-----\n"
            f"NEXT=changeme\n# AFTER={C}\n")

    out = mask(text)

    assert C not in out
    assert out.split("\n") == ["# SSH_KEY=[value withheld]", "", "", "", "NEXT=changeme", "# AFTER=[value withheld]"]


def test_a_commented_quoted_value_goes_on_over_the_comment_lines_that_follow_and_no_further():
    text = f'# TOKEN="first\n# {C}\n# last"\nNEXT=1\n'
    assert mask(text).split("\n") == ["# TOKEN=[value withheld]", "", "", "NEXT=1"]

    open_ended = f"# TOKEN='never closed\nNEXT=1\nLAST={C}\n"
    out = mask(open_ended)
    assert C not in out and out.split("\n")[1] == "NEXT=1", "a stray quote in a comment does not swallow the file"


def test_a_comment_inside_a_value_that_is_withheld_is_withheld_with_it():
    text = f'KEY="a # b\n{C}"\nNEXT=changeme\n'

    assert mask(text).split("\n") == ["KEY=[value withheld]", "", "NEXT=changeme"]


def test_the_masker_is_linear_on_one_very_long_comment_line():
    import time

    long_line = "# " + "word=" * 40_000 + "\n" + "# " + "=" * 200_000 + "\n" + "# " + "a" * 200_000 + "\n"
    t0 = time.perf_counter()
    out = mask(long_line)
    elapsed = time.perf_counter() - t0

    assert out.count("\n") == 2
    assert elapsed < 5, f"{elapsed:.1f}s"


def test_an_example_env_read_through_the_readers_shows_none_of_it(tmp_path):
    from src.mcp_server.howto import engine
    from src.retrieval.tier3_code import CodeReader

    root = tmp_path / "acme-app"
    root.mkdir()
    (root / ".env.example").write_bytes(
        f"# Application\r\nPORT=8080  # default port\r\n# OLD_KEY={C}\r\nKEY=changeme  # was sk-live-{C}\r\n"
        f"OTHER=changeme\x0cSECRET={C}\n".encode())

    for shown in (CodeReader().read_full_file(root, ".env.example").content, engine._read(root, ".env.example")):
        assert C not in shown
        assert "PORT=8080  # default port" in shown and "# Application" in shown
        assert "OLD_KEY=[value withheld]" in shown and "SECRET=[value withheld]" in shown
