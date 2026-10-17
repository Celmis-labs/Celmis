"""A SKILL.md whose description holds ``: `` unquoted is read the way Claude Code
reads it (one line of text), and a front matter that is still not YAML names
the line of the FILE, the key and the fix."""

from __future__ import annotations

import pytest

from src.knowledge.ingest import IngestError, ingest_files


def _skill(front: str) -> list[tuple[str, bytes]]:
    return [("probe/SKILL.md", f"---\n{front}\n---\n# Probe\n\nBody.\n".encode())]


def test_an_unquoted_colon_in_the_description_is_part_of_the_text():
    result = ingest_files(_skill("name: probe\ndescription: Does a thing: with a colon inside"))
    (skill,) = [f for f in result.files if f.path.endswith("SKILL.md")]
    assert skill.description == "Does a thing: with a colon inside"


def test_quoted_and_block_values_are_read_as_before():
    result = ingest_files(_skill('name: probe\ndescription: "Quoted: as before"\ntags:\n  - a\n  - b'))
    (skill,) = [f for f in result.files if f.path.endswith("SKILL.md")]
    assert skill.description == "Quoted: as before"


def test_a_front_matter_that_is_still_not_yaml_names_the_file_line_the_key_and_the_fix():
    with pytest.raises(IngestError) as caught:
        ingest_files(_skill("name: probe\ndescription: fine\ntags: [unclosed"))
    message = str(caught.value)
    assert caught.value.code == "bad_front_matter"
    assert "SKILL.md" in message and "quotes" in message
    assert "(line 4" in message or "(line 5" in message
