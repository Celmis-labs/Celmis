"""Tests for VueExtractor — Phase 5b.

Vue SFC: <template>, <script>, <script setup>, <style>.
Скрипт-блоки re-парсяться через TS injection — line numbers зберігаються
відносно .vue файлу автоматично (через included_ranges на новому Parser).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from src.indexing.graph.languages.vue import (
    VueExtractor,
    _detect_script_lang,
)

# ─── helpers ────────────────────────────────────────────────────────


@pytest.fixture
def extract():
    """Helper: extract from inline Vue source."""
    def _extract(source: str, file_path: str = "test.vue"):
        ext = VueExtractor()
        return ext.extract(Path(file_path), source.encode("utf-8"))
    return _extract


# ─── lang detection ─────────────────────────────────────────────────


def test_detect_lang_ts():
    assert _detect_script_lang({"lang": "ts"}) == "typescript"
    assert _detect_script_lang({"lang": "typescript"}) == "typescript"


def test_detect_lang_tsx():
    assert _detect_script_lang({"lang": "tsx"}) == "tsx"


def test_detect_lang_default():
    assert _detect_script_lang({}) == "javascript"
    assert _detect_script_lang({"setup": True}) == "javascript"


# ─── basic SFC parsing ──────────────────────────────────────────────


def test_empty_sfc_no_symbols(extract):
    """SFC без script — порожній."""
    res = extract("<template><div>hello</div></template>")
    assert res.symbols == []
    assert res.parse_errors == []


def test_simple_options_api(extract):
    src = textwrap.dedent('''
        <template>
            <div>{{ msg }}</div>
        </template>
        <script>
        export default {
            data() {
                return { msg: 'hi' };
            }
        };
        </script>
    ''')
    res = extract(src)
    # export default не зареєструє data() як top-level symbol —
    # реєструється "default" як export_default symbol (із object expression value)
    names = [s.name for s in res.symbols]
    assert "default" in names


def test_script_with_typed_function(extract):
    src = textwrap.dedent('''
        <template></template>
        <script lang="ts">
        export function helper(x: number): string {
            return String(x);
        }
        </script>
    ''')
    res = extract(src)
    helpers = [s for s in res.symbols if s.name == "helper"]
    assert len(helpers) == 1
    assert helpers[0].is_exported is True
    assert helpers[0].kind == "function"


# ─── <script setup> ─────────────────────────────────────────────────


def test_script_setup_top_level_marked_exported(extract):
    """У <script setup> усі top-level декларації автоматично exposed."""
    src = textwrap.dedent('''
        <template></template>
        <script setup>
        const counter = 0;
        function increment() { counter++; }
        </script>
    ''')
    res = extract(src)
    syms = {s.name: s for s in res.symbols}
    assert "counter" in syms
    assert "increment" in syms
    # <script setup> → всі top-level автоматично exported
    assert syms["counter"].is_exported is True
    assert syms["increment"].is_exported is True


def test_script_setup_lang_ts(extract):
    src = textwrap.dedent('''
        <template></template>
        <script setup lang="ts">
        import { ref } from 'vue';

        interface Props { name: string }

        const greeting = ref<string>('hello');
        function greet(): string {
            return greeting.value;
        }
        </script>
    ''')
    res = extract(src)
    names = {s.name for s in res.symbols}
    assert "greeting" in names
    assert "greet" in names

    # Imports
    targets = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
    assert "vue::ref" in targets


def test_script_setup_macros_as_calls(extract):
    """defineProps, defineEmits — це call_expression, тому реєструються як CALLS edges."""
    src = textwrap.dedent('''
        <template></template>
        <script setup lang="ts">
        const props = defineProps<{ msg: string }>();
        const emit = defineEmits<{ click: [] }>();
        </script>
    ''')
    res = extract(src)
    call_targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
    assert "defineProps" in call_targets
    assert "defineEmits" in call_targets


def test_two_scripts_options_plus_setup(extract):
    """Vue 3 hybrid: <script> + <script setup>."""
    src = textwrap.dedent('''
        <template></template>
        <script>
        export const SHARED = 42;
        </script>
        <script setup>
        const local = 1;
        </script>
    ''')
    res = extract(src)
    names = {s.name for s in res.symbols}
    assert "SHARED" in names
    assert "local" in names


# ─── line-number mapping (включні діапазони) ────────────────────────


def test_line_numbers_relative_to_vue_file(extract):
    """Декларація у <script> мусить мати line number відносно .vue файлу,
    а не відносно script content. Підтверджує що included_ranges працює."""
    src = (
        "<template>\n"             # line 1
        "    <div>x</div>\n"       # line 2
        "</template>\n"            # line 3
        "\n"                       # line 4
        "<script setup>\n"         # line 5
        "const onLine6 = 1;\n"     # line 6
        "</script>\n"              # line 7
    )
    res = extract(src)
    on_line = next(s for s in res.symbols if s.name == "onLine6")
    assert on_line.start_line == 6


# ─── imports у script ───────────────────────────────────────────────


def test_imports_extracted_from_script_section(extract):
    src = textwrap.dedent('''
        <template></template>
        <script setup lang="ts">
        import { ref, computed } from 'vue';
        import ordersController from 'src/models/Orders';
        </script>
    ''')
    res = extract(src)
    targets = sorted(e.raw_target for e in res.edges if e.kind == "IMPORTS")
    assert "vue::ref" in targets
    assert "vue::computed" in targets
    assert "src/models/Orders::default" in targets


# ─── self-closing / no script ──────────────────────────────────────


def test_self_closing_script_safe(extract):
    """<script />  — empty self-closed; не падає."""
    src = '<template></template><script />'
    res = extract(src)
    assert res.symbols == []
    assert res.edges == []


def test_template_only_safe(extract):
    """<template>...</template> без жодного <script>."""
    src = textwrap.dedent('''
        <template>
            <div>just html</div>
        </template>
    ''')
    res = extract(src)
    assert res.symbols == []


# ─── invalid Vue does not crash ─────────────────────────────────────


def test_malformed_vue_does_not_crash(extract):
    res = extract("<template>broken<script>let x =")
    # parse_errors може бути, але не має краш'у
    assert isinstance(res.symbols, list)


# ─── DoD: symbol attribution для Vue ───────────────────────────────


def test_dod_vue_extractor_full_cycle(extract):
    """Повний real-world фрагмент Vue SFC: imports + script setup + macros + emit."""
    src = textwrap.dedent('''
        <template>
            <button @click="handleClick">{{ title }}</button>
        </template>

        <script setup lang="ts">
        import { ref } from 'vue';
        import { useStore } from 'vuex';

        defineProps<{ title: string }>();
        const emit = defineEmits<{ submit: [] }>();

        const store = useStore();
        const counter = ref(0);

        function handleClick() {
            counter.value++;
            emit('submit');
        }
        </script>
    ''')
    res = extract(src, "MyComponent.vue")

    # Symbols expected
    names = {s.name for s in res.symbols}
    assert "emit" in names
    assert "store" in names
    assert "counter" in names
    assert "handleClick" in names

    # All from <script setup> exported
    for sym in res.symbols:
        if sym.file == "MyComponent.vue":
            assert sym.is_exported is True

    # Imports
    imp_targets = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
    assert "vue::ref" in imp_targets
    assert "vuex::useStore" in imp_targets

    # Call edges
    call_targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
    assert "defineProps" in call_targets
    assert "defineEmits" in call_targets
    assert "useStore" in call_targets
    assert "ref" in call_targets

    # handleClick body should have emit and counter.value++
    handler_calls = [
        e for e in res.edges
        if e.kind == "CALLS" and e.from_id.endswith("::handleClick")
    ]
    handler_targets = {e.raw_target for e in handler_calls}
    assert "emit" in handler_targets


# ─── real-world coverage smoke test (skip if no repo) ──────────────


@pytest.mark.skipif(
    not os.environ.get("CELMIS_REAL_REPO"),
    reason="CELMIS_REAL_REPO is not set (path to a real frontend clone)",
)
def test_real_vue_coverage_high():
    """Coverage check: на ≥30 sample Vue файлах clean rate >=85%.
    Поточне вимірювання дає 100% clean parse — це робить @vue/compiler-sfc
    fallback непотрібним.
    """
    repo = Path(os.environ["CELMIS_REAL_REPO"]).expanduser()
    extractor = VueExtractor(repo_root=repo)
    files = [f for f in repo.rglob("*.vue") if "node_modules" not in f.parts][:50]
    if not files:
        # An empty list used to reach `clean / len(files)` and raise
        # ZeroDivisionError, which reads as a broken extractor rather than
        # "there was nothing to measure".
        pytest.skip("no .vue files in CELMIS_REAL_REPO")
    clean = sum(1 for f in files if not extractor.extract(f).parse_errors)
    rate = clean / len(files)
    assert rate >= 0.85, f"Vue coverage {rate:.0%} < 85% threshold"
