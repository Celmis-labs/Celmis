"""A snippet is read with context lines above the asked line; its link kept
pointing at the first context line, so every ``#L`` in a generated note landed
two or three lines above the definition it named."""

from __future__ import annotations

from pathlib import Path

from src.retrieval.tier3_code import CodeReader

SOURCE = "\n".join(f"line_{i} = {i}" for i in range(1, 21)) + "\n"


def _block(tmp_path: Path, line: int, context: int) -> str:
    (tmp_path / "m.py").write_text(SOURCE)
    bundle = CodeReader().read_locations(
        tmp_path, [("m.py", line, line + 1)], budget_tokens=10_000,
        context_lines=context, redact_content=False,
    )
    snippet = bundle.snippets[0]
    assert snippet.start_line == max(1, line - context)  # the context is still read
    return snippet.as_markdown_block().splitlines()[0]


def test_the_header_names_the_asked_line(tmp_path: Path) -> None:
    assert _block(tmp_path, 10, 2) == "### 📍 [m.py:10](m.py#L10)"


def test_near_the_top_of_the_file_the_header_still_names_the_asked_line(tmp_path: Path) -> None:
    assert _block(tmp_path, 1, 3) == "### 📍 [m.py:1](m.py#L1)"
