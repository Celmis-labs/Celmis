"""Tests for CodeChunker — Phase 7a.

Перевіряє: language detection, chunk emission, line-number derivation,
metadata payload format.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from src.indexing.vectors.chunker import CodeChunk, CodeChunker, lang_for

# ─── language detection ─────────────────────────────────────────────


def test_lang_for_typescript():
    assert lang_for("foo.ts") == "typescript"
    assert lang_for("foo.tsx") == "tsx"


def test_lang_for_javascript():
    for ext in (".js", ".jsx", ".cjs", ".mjs"):
        assert lang_for(f"x{ext}") == "javascript"


def test_lang_for_vue():
    assert lang_for("Component.vue") == "vue"


def test_lang_for_unsupported():
    assert lang_for("README.md") is None
    assert lang_for("data.json") is None
    assert lang_for("noext") is None


# ─── chunking ──────────────────────────────────────────────────────


@pytest.fixture
def chunker():
    return CodeChunker()


def test_empty_source(chunker, tmp_path):
    f = tmp_path / "x.ts"
    f.write_text("")
    chunks = chunker.chunk_file(f)
    assert chunks == []


def test_unsupported_extension(chunker, tmp_path):
    f = tmp_path / "data.json"
    f.write_text('{"x": 1}')
    chunks = chunker.chunk_file(f)
    assert chunks == []


def test_simple_typescript_chunk(chunker, tmp_path):
    src = textwrap.dedent("""
        import { ref } from 'vue';

        export function counter(initial: number) {
            const count = ref(initial);
            const increment = () => count.value++;
            return { count, increment };
        }

        export class MyClass {
            doIt() { return 42; }
        }
    """).strip()
    f = tmp_path / "counter.ts"
    f.write_text(src)
    chunks = chunker.chunk_file(f)

    assert len(chunks) >= 1
    for c in chunks:
        assert isinstance(c, CodeChunk)
        assert c.text
        assert c.file == str(f)
        assert c.start_line >= 1
        assert c.end_line >= c.start_line
        assert c.language == "typescript"
        assert c.chunk_index >= 0


def test_chunk_id_format(chunker, tmp_path):
    f = tmp_path / "y.ts"
    f.write_text("export function f() { return 1; }")
    chunks = chunker.chunk_file(f, rel_path="src/y.ts")
    assert len(chunks) == 1
    cid = chunks[0].chunk_id()
    assert cid.startswith("src/y.ts:")
    assert ":" in cid


def test_payload_excludes_text(chunker, tmp_path):
    """Payload не містить text — text ходить у sparse vector окремо."""
    f = tmp_path / "z.ts"
    f.write_text("function x() {}")
    chunks = chunker.chunk_file(f, rel_path="z.ts")
    payload = chunks[0].to_payload()
    assert "text" not in payload
    assert "file" in payload
    assert "language" in payload


def test_relative_path_used(chunker, tmp_path):
    """Якщо rel_path передано — використовується (не absolute path)."""
    f = tmp_path / "deep/path/file.ts"
    f.parent.mkdir(parents=True)
    f.write_text("function f() {}")
    chunks = chunker.chunk_file(f, rel_path="src/deep/path/file.ts")
    assert chunks[0].file == "src/deep/path/file.ts"


def test_long_file_produces_multiple_chunks(chunker, tmp_path):
    """Файл з 200+ рядків має давати >1 chunk при default chunk_lines=40."""
    lines = [f"function f{i}() {{ return {i}; }}" for i in range(200)]
    src = "\n".join(lines)
    f = tmp_path / "big.ts"
    f.write_text(src)
    chunks = chunker.chunk_file(f)
    assert len(chunks) > 1


def test_line_numbers_monotonic(chunker, tmp_path):
    """Послідовні chunks мають неспадаючі start_line."""
    lines = [f"// line {i}\nfunction f{i}() {{}}\n" for i in range(60)]
    src = "".join(lines)
    f = tmp_path / "ml.ts"
    f.write_text(src)
    chunks = chunker.chunk_file(f)
    starts = [c.start_line for c in chunks]
    assert starts == sorted(starts)


def test_vue_sfc_chunked(chunker, tmp_path):
    src = textwrap.dedent("""
        <template>
            <div>{{ msg }}</div>
        </template>
        <script setup lang="ts">
        import { ref } from 'vue';
        const msg = ref('hi');
        function update() { msg.value = 'updated'; }
        </script>
    """).strip()
    f = tmp_path / "Hi.vue"
    f.write_text(src)
    chunks = chunker.chunk_file(f)
    assert len(chunks) >= 1
    assert chunks[0].language == "vue"


# ─── DoD: chunker on real-world file ────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("CELMIS_REAL_REPO"),
    reason="CELMIS_REAL_REPO is not set (path to a real frontend clone)",
)
def test_chunker_on_real_typescript_file(chunker):
    """The chunker survives the biggest real TypeScript file in the repo.

    It used to name one file inside one customer's repository. The property was
    never about that file — it is "a real several-hundred-line module does not
    break the chunker" — so the biggest .ts file in whatever CELMIS_REAL_REPO
    points at proves the same thing, and points at anything.
    """
    repo = Path(os.environ["CELMIS_REAL_REPO"]).expanduser()
    candidates = [f for f in repo.rglob("*.ts")
                  if "node_modules" not in f.parts and f.is_file()]
    if not candidates:
        pytest.skip("no .ts file in CELMIS_REAL_REPO")
    pc = max(candidates, key=lambda f: f.stat().st_size)
    chunks = chunker.chunk_file(pc, rel_path=str(pc.relative_to(repo)))
    assert len(chunks) > 1
    # All chunks have valid metadata
    for c in chunks:
        assert c.start_line >= 1
        assert c.end_line >= c.start_line
        assert c.language == "typescript"
        assert c.text


@pytest.mark.skipif(
    not os.environ.get("CELMIS_REAL_REPO"),
    reason="CELMIS_REAL_REPO is not set (path to a real frontend clone)",
)
def test_chunker_on_real_vue_file(chunker):
    repo = Path(os.environ["CELMIS_REAL_REPO"]).expanduser()
    sample = next(
        (f for f in repo.rglob("*.vue") if "node_modules" not in f.parts),
        None,
    )
    if sample is None:
        pytest.skip("no .vue file found")
    chunks = chunker.chunk_file(sample)
    assert len(chunks) >= 1
    assert chunks[0].language == "vue"


# ─── a grammar that refuses a file must not delete it from search ────


def test_a_file_the_grammar_refuses_is_still_chunked(chunker, monkeypatch):
    """Returning [] on a splitter failure is how a file with a full graph
    becomes unfindable by "ask the code". A line window is a worse chunk than
    a syntactic one and an infinitely better one than no chunk at all."""
    def _refuse(_language):
        raise RuntimeError("grammar says no")

    monkeypatch.setattr(chunker, "_get_splitter", _refuse)
    source = "\n".join(f"line {i}" for i in range(1, 121))

    chunks = chunker.chunk_file("app/models/user.rb", source=source)

    assert chunks, "the file fell out of search entirely"
    assert all(c.language == "ruby" for c in chunks)
    assert all(c.text.strip() for c in chunks)


def test_the_fallback_chunks_still_carry_usable_line_numbers(chunker, monkeypatch):
    """The line numbers are what a retrieval hit shows the user; a fallback
    that returns text without a location is a citation nobody can follow."""
    monkeypatch.setattr(
        chunker, "_get_splitter", lambda _l: (_ for _ in ()).throw(RuntimeError("no")),
    )
    source = "\n".join(f"line {i}" for i in range(1, 121))

    chunks = chunker.chunk_file("a.rb", source=source)

    assert chunks[0].start_line == 1
    for c in chunks:
        assert 1 <= c.start_line <= c.end_line <= 120
    assert max(c.end_line for c in chunks) >= 110, "the tail of the file was dropped"


def test_the_fallback_leaves_an_empty_file_empty(chunker, monkeypatch):
    monkeypatch.setattr(
        chunker, "_get_splitter", lambda _l: (_ for _ in ()).throw(RuntimeError("no")),
    )

    assert chunker.chunk_file("a.rb", source="\n\n   \n") == []


def test_a_file_in_no_known_language_is_still_skipped(chunker):
    """The fallback is for a language we claim and cannot split, not for
    everything on disk: a PNG or a lockfile must not become search noise."""
    assert chunker.chunk_file("README.md", source="# hello\n\ntext") == []
    assert chunker.chunk_file("yarn.lock", source="a@1.0.0:\n  version 1") == []


def test_the_windows_overlap_so_a_symbol_on_a_seam_survives(chunker, monkeypatch):
    """Two adjacent windows with no overlap can cut a function in half and
    leave neither chunk able to answer for it."""
    monkeypatch.setattr(
        chunker, "_get_splitter", lambda _l: (_ for _ in ()).throw(RuntimeError("no")),
    )
    source = "\n".join(f"line {i}" for i in range(1, 201))

    chunks = chunker.chunk_file("a.rb", source=source)

    assert len(chunks) > 1
    assert any(
        later.start_line <= earlier.end_line
        for earlier, later in zip(chunks, chunks[1:], strict=False)
    ), "the fallback windows do not overlap"
