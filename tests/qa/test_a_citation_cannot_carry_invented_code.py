"""A confident citation to a real file, carrying code that is not in it.

THE DEFECT. `verify_citations` checked two things: does the file exist, and is
the line number inside it. Its own docstring names the danger as "the prose
looks authoritative and the link looks precise, but the line doesn't exist" —
which is only half of it.

In production, with `include_code` switched off, the model wrote:

    📍 [github_…-payments/src/settlement.py](src/settlement.py#L1)
    ```python
      1  def process_settlement(transaction_id, amount):
      2      # Logic to handle settlement
      3      print(f"Processing settlement for {transaction_id}…")
    ```

`process_settlement` exists in no repository. `src/settlement.py` contains
`split_settlement` and `settle`. Every check passed — the file is real, line 1
is in range — and the answer was reported with `citations_invalid: 0`.

THE CHECK IS DELIBERATELY CONSERVATIVE. It fires only when a block contains
real code and NOT ONE substantive line occurs anywhere in the file. Partial
quotes, re-indented quotes, elided quotes and quotes from elsewhere in the file
all pass. `suggestion` and `diff` fences are exempt outright — their whole
purpose is to differ from what is there.
"""

from __future__ import annotations

import pytest

from src.qa.citations import verify_citations

REAL = '''"""Settlement maths."""


def split_settlement(total_cents: int, parties: list[str]) -> dict[str, int]:
    if not parties:
        raise ValueError("cannot split a settlement between zero parties")
    share = total_cents // len(parties)
    return {p: share for p in parties}
'''


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = tmp_path / "github_acme-payments"
    (root / "src").mkdir(parents=True)
    (root / "src" / "settlement.py").write_text(REAL)

    from src.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(type(settings), "repo_path",
                        lambda self, slug: tmp_path / slug, raising=False)
    return "github_acme-payments"


def cite(body: str, lang: str = "python", line: int = 1) -> str:
    return (
        f"Here it is:\n\n"
        f"📍 [src/settlement.py](github_acme-payments/src/settlement.py#L{line})\n\n"
        f"```{lang}\n{body}```\n"
    )


def test_invented_code_under_a_real_path_is_caught(repo):
    text = cite("  1  def process_settlement(transaction_id, amount):\n"
                "  2      # Logic to handle settlement\n"
                "  3      print(f\"Processing settlement for {transaction_id}\")\n")

    report = verify_citations(text, repos=[repo])

    assert report.total == 1
    assert report.citations[0].status == "fabricated_snippet"
    assert len(report.invalid) == 1


def test_a_real_quote_passes(repo):
    text = cite("    share = total_cents // len(parties)\n"
                "    return {p: share for p in parties}\n", line=7)

    assert verify_citations(text, repos=[repo]).citations[0].status == "ok"


def test_a_real_quote_with_the_files_line_numbers_passes(repo):
    """The model renders quoted source with the file's own numbering in front.
    Not stripping that would make every genuine quote look fabricated."""
    text = cite("7      share = total_cents // len(parties)\n", line=7)

    assert verify_citations(text, repos=[repo]).citations[0].status == "ok"


def test_a_partial_quote_passes(repo):
    """One matching line is enough — the check is for wholesale invention."""
    text = cite("    share = total_cents // len(parties)\n"
                "    ...\n", line=7)

    assert verify_citations(text, repos=[repo]).citations[0].status == "ok"


def test_a_suggestion_block_is_exempt(repo):
    """A proposed replacement is SUPPOSED to differ from the file."""
    text = cite("    remainder = total_cents % len(parties)\n"
                "    return {p: share + (1 if i < remainder else 0)}\n",
                lang="suggestion", line=7)

    assert verify_citations(text, repos=[repo]).citations[0].status == "ok"


def test_a_diff_block_is_exempt(repo):
    text = cite("-    return {p: share for p in parties}\n"
                "+    return distribute(share, parties, remainder)\n",
                lang="diff", line=7)

    assert verify_citations(text, repos=[repo]).citations[0].status == "ok"


def test_a_citation_with_no_code_block_is_untouched(repo):
    text = "See [src/settlement.py](github_acme-payments/src/settlement.py#L7) for the split."

    assert verify_citations(text, repos=[repo]).citations[0].status == "ok"


def test_trivial_lines_alone_never_trigger_the_check(repo):
    """A block of braces and ellipses carries no evidence either way, and
    flagging it would be noise."""
    text = cite("}\n...\n", line=7)

    assert verify_citations(text, repos=[repo]).citations[0].status == "ok"


def test_a_bad_line_still_wins_over_the_snippet_check(repo):
    """The cheaper, more certain failure keeps its own name."""
    text = cite("def process_settlement(transaction_id, amount):\n", line=9999)

    assert verify_citations(text, repos=[repo]).citations[0].status == "bad_line"


# ─── the block the window used to skip ───────────────────────────────


LONG_INVENTION = "\n".join(
    f"    result_{i} = compute_settlement_step_{i}(transaction, ledger_state)"
    for i in range(30)
)


def test_a_long_invented_block_is_still_checked(repo):
    """The case the check exists for, and the one the window used to skip.

    `_FENCE_RE` had to match a COMPLETE fence inside 400 characters, so a
    wholly invented function — which is long by nature — had its closing ```
    fall outside the window and was never examined. One of fifteen citations
    in a single measured session escaped exactly that way.
    """
    text = cite(f"def process_settlement(transaction, ledger_state):\n{LONG_INVENTION}\n")

    report = verify_citations(text, repos=[repo])

    assert report.citations[0].status == "fabricated_snippet"


def test_a_long_genuine_quote_still_passes(repo):
    """The other direction: reading further must not start accusing real
    quotes just because they are long."""
    padding = "\n".join(f"    # note {i}" for i in range(30))
    text = cite(f"    share = total_cents // len(parties)\n{padding}\n", line=7)

    assert verify_citations(text, repos=[repo]).citations[0].status == "ok"


def test_an_unterminated_fence_is_checked_to_the_end(repo):
    """A model that opened a block and never closed it has still shown the
    reader code, and that code still has to be real."""
    text = (
        "Here:\n\n"
        "📍 [src/settlement.py](github_acme-payments/src/settlement.py#L1)\n\n"
        "```python\ndef process_settlement(transaction_id, amount):\n"
        "    print('processing')\n"
    )

    assert verify_citations(text, repos=[repo]).citations[0].status == "fabricated_snippet"


def test_a_block_far_from_the_citation_is_not_blamed_on_it(repo):
    """The window still does its job: it bounds how far away a block may
    START, so an unrelated later block is not attributed to this citation."""
    filler = "\n".join(f"Some prose line {i}." for i in range(40))
    text = (
        "📍 [src/settlement.py](github_acme-payments/src/settlement.py#L7)\n\n"
        f"{filler}\n\n"
        "```python\ndef something_else_entirely():\n    pass\n```\n"
    )

    assert verify_citations(text, repos=[repo]).citations[0].status == "ok"
