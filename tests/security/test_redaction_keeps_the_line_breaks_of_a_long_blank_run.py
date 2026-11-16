"""Cutting a long run of blanks never joins two lines.

A comment padded with blanks, a CRLF and the next line's indentation make a
run of more than 64 whitespace characters. Cutting it to its first 64 dropped
the line break, so the redacted body had one line less and every line number a
dev tool printed after it was off by one.
"""

from __future__ import annotations

from src.mcp_server.dev_profile import emit
from src.security.mcp_redact import bound_whitespace, redact_for_mcp


def test_a_padded_comment_before_a_crlf_keeps_its_line_break_and_the_next_indent():
    lines = ["// note" + " " * 60 + "\r", "\t" * 7 + "total = 1\r", "end\r"]
    out = bound_whitespace("\n".join(lines)).split("\n")
    assert len(out) == 3
    assert out[1] == "\t" * 7 + "total = 1\r"
    assert out[2] == "end\r"


def test_every_cut_run_stays_within_64_characters_unless_it_is_only_line_breaks():
    import re

    text = "a" + " " * 30 + "\n" * 3 + " " * 40 + "\n" + "\t" * 90 + "b" + " " * 200 + "c"
    out = bound_whitespace(text)
    assert out.count("\n") == text.count("\n")
    assert max(len(r) for r in re.findall(r"\s+", out)) <= 64
    flood = "x" + "\n" * 100 + "y"
    assert bound_whitespace(flood) == flood


def test_a_run_without_a_line_break_is_still_cut_to_64():
    assert bound_whitespace("a" + " " * 1000 + "b") == "a" + " " * 64 + "b"


def test_a_secret_across_a_long_run_with_a_line_break_is_still_masked():
    text = "password =" + " " * 60 + "\n" + " " * 40 + "hunter2hunter2"
    out, _ = redact_for_mcp(text)
    assert "hunter2hunter2" not in out
    assert out.count("\n") == 1


def test_the_dev_profile_redaction_keeps_the_line_count_of_a_crlf_body():
    body = []
    for i in range(50):
        body.append(f"\t\t// step {i}" + " " * (40 + i) + "\r")
        body.append("\t" * 8 + f"value{i} = value{i} + 1;\r")
    out = emit.redact_text("\n".join(body)).split("\n")
    assert len(out) == len(body)
    assert all(out[k].endswith(f"value{k // 2} = value{k // 2} + 1;\r")
               for k in range(1, len(body), 2))
