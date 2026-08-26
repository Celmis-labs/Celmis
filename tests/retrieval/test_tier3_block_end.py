"""Тести для _detect_block_end — перевіряє що JS-евристика
не зупиняється на верхньорівневих імпортах.

Регресія, яку ловимо: для useEstimate.js (start=1, end=None) старий
код повертав end=2 — закриваючий `}` у `import { computed } from 'vue'`,
тобто LLM бачив тільки рядки імпорту замість тіла функції.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from src.retrieval.tier3_code import (
    CodeReader,
    _detect_block_end,
    _is_js_noise_line,
)

USE_ESTIMATE_JS = textwrap.dedent("""\
    // useEstimate.js

    import ordersController from 'src/models/core/domains/Orders/OrdersController.ts';
    import { computed } from 'vue';
    import { useStore } from 'vuex';

    // Символ валюти за замовчуванням
    export const currencySign = computed(
      () => ordersController.activeOrder?.value?.totals?.currency?.sign || '¤',
    );

    export function useEstimate() {
      const store = useStore();
      const estimateStatus = computed(() => store.state.ui.estimate.status);
      return {
        estimateStatus
      };
    }
    """)


def test_is_js_noise_line_recognises_imports_and_comments() -> None:
    assert _is_js_noise_line("import { x } from 'm';")
    assert _is_js_noise_line("  // коментар")
    assert _is_js_noise_line("/* блок */")
    assert _is_js_noise_line("")
    assert _is_js_noise_line("export { foo } from 'bar';")
    # реальний код — не шум
    assert not _is_js_noise_line("export const currencySign = computed(")
    assert not _is_js_noise_line("function useEstimate() {")
    assert not _is_js_noise_line("const x = 5;")


def test_detect_block_end_skips_top_level_imports() -> None:
    """start=1 для JS — має пропустити imports і знайти перший реальний блок."""
    lines = USE_ESTIMATE_JS.splitlines()
    end = _detect_block_end(lines, start=1, suffix=".js")
    # перший реальний блок — `export const currencySign = computed(...)` з закриваючою `)`
    # на рядку зі стрілкою. Головне — НЕ повертає 4 (рядок імпорту).
    assert end >= 11, f"end={end} попало на імпорт замість тіла"


def test_detect_block_end_finds_function_body(tmp_path: Path) -> None:
    """Якщо start вказує на саму function — має знайти кінець (closing brace)."""
    lines = USE_ESTIMATE_JS.splitlines()
    # `export function useEstimate() {` — рядок 12 (1-based після textwrap)
    func_line = next(i + 1 for i, ln in enumerate(lines) if "function useEstimate" in ln)
    end = _detect_block_end(lines, start=func_line, suffix=".js")
    # closing `}` має бути після останнього `return {...}` блоку
    assert end > func_line + 4
    # перевіряємо що блок реально містить return
    block = "\n".join(lines[func_line - 1:end])
    assert "estimateStatus" in block
    assert "return" in block


def test_code_reader_reads_useEstimate_correctly(tmp_path: Path) -> None:
    """E2E: CodeReader.read_locations з (file, 1, None) повертає тіло, не імпорти."""
    fake_file = tmp_path / "useEstimate.js"
    fake_file.write_text(USE_ESTIMATE_JS)

    reader = CodeReader()
    bundle = reader.read_locations(
        repo_path=tmp_path,
        locations=[("useEstimate.js", 1, None)],
        budget_tokens=10_000,
        context_lines=0,
        redact_content=False,
    )
    assert bundle.snippets, "має бути хоча б один snippet"
    content = bundle.snippets[0].content
    # Регресія: раніше тут було тільки 'import { computed } from vue'
    # і нічого більше. Зараз має містити реальну логіку.
    assert "computed(" in content or "useEstimate" in content
