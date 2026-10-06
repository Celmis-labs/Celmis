"""A library named in a comment or a docstring is not a use of it.

Found on a real service: the first mention of the JWKS client was a comment at the
top of the file, so the "auth" slice showed that file's header (constants and prose)
instead of the code that verifies the token 240 lines further down.
"""

from __future__ import annotations

from src.mcp_server.howto import detectors as D
from src.mcp_server.howto.engine import comment_lines, score_file

PY_COMMENT_FIRST = '''\
"""Token checks. We cannot use PyJWKClient directly, see below."""

import httpx
import jwt

# PyJWKClient cannot load keys behind the proxy, so we fetch them ourselves.
HEADERS = {"Accept": "application/json"}


def keys(url):
    return httpx.get(url, headers=HEADERS).json()


def verify(token, key):
    return jwt.decode(token, key, algorithms=["RS256"])
'''


def test_comment_lines_finds_hash_comments_and_docstrings_but_not_code() -> None:
    lines = comment_lines(PY_COMMENT_FIRST, ".py")
    assert {1, 6} <= lines
    assert not ({3, 4, 7, 10, 11, 14, 15} & lines)


def test_a_multiline_string_that_is_assigned_does_not_swallow_the_rest_of_the_file() -> None:
    text = 'QUERY = """\nSELECT 1\n"""\n\n\ndef run(c):\n    return c.execute(QUERY)\n'
    assert not (comment_lines(text, ".py") & {6, 7})


def test_block_comments_and_line_comments_are_found_in_c_like_languages() -> None:
    text = "/* a\n * b\n */\n// c\nconst x = 1;\nfoo(); // tail\n"
    assert comment_lines(text, ".ts") == {1, 2, 3, 4}


def test_the_slice_anchor_is_the_code_that_uses_the_library_not_the_comment() -> None:
    cand = score_file(D.DETECTORS["auth"], "app/oidc.py", PY_COMMENT_FIRST)
    assert cand is not None
    assert cand.line >= 14, f"anchored on line {cand.line}, a comment or docstring"
    assert all(h.line not in (1, 6) for h in cand.hits)


def test_a_file_that_names_the_library_only_in_comments_is_not_a_candidate() -> None:
    text = "# PyJWKClient is not used here\nVALUE = 1\n"
    assert score_file(D.DETECTORS["auth"], "app/notes.py", text) is None
