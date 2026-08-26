"""Eval скрипт для оцінки якості retrieval після фіксів tier3 + vault fallback.

Прогоняє набір питань про реальний репозиторій ($CELMIS_REAL_REPO) і рахує метрики:
  - кількість файлів, прочитаних tier3
  - кількість markdown code-блоків у відповіді
  - частка "імпорт-only" блоків (антипатерн який ми лікували)
  - чи зустрічаються очікувані keywords (sanity check)

Набір питань (CASES нижче) — приклад для демонстраційного репозиторію.
Він не універсальний: підмініть питання й очікувані символи на свої під той
репозиторій, який реально оцінюєте.

Запуск (repo обов'язковий — через прапорець або env):
  CELMIS_REAL_REPO=<repo-slug> python -m scripts.eval_qa_quality
  python -m scripts.eval_qa_quality --repo <repo-slug>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass


@dataclass
class CaseResult:
    question: str
    route: str
    qtype: str
    files_read: int
    code_blocks: int
    import_only_blocks: int
    body_blocks: int  # code blocks з реальним тілом (не лише імпорти)
    expected_keywords_hit: int
    expected_keywords_total: int
    tokens_in: int
    tokens_out: int
    elapsed_s: float
    verdict: str  # PASS | WEAK | FAIL


# ПРИКЛАД набору. Питання і expected-символи прив'язані до конкретного репозиторію —
# замініть CASES на свої перед прогоном проти власного коду. Форма важлива, не зміст:
# кожен case — одне питання + список символів, які мають зустрітись у відповіді, і
# кожен покриває свій шлях retrieval (широкий flow / exact symbol / файл+метод /
# ланцюжок викликів). Нижче — набір для демо-репозиторію ordering/pricing.
CASES = [
    {
        # Широкий flow-question: очікуємо, що відповідь пройде через кілька
        # символів однієї фічі, включно з приватними прапорцями.
        "question": "Опиши логіку формування даних для запиту на розрахунок вартості замовлення?",
        "expected": ["Order", "_isDisabledForDraft", "configuredProducts",
                     "_isEnabledEstimate", "useEstimate", "totals"],
    },
    {
        # Exact symbol: перевіряємо, що дістали тіло композабла, а не лише імпорти.
        "question": "Як працює useEstimate?",
        "expected": ["useEstimate", "computed", "useStore", "estimateOperation", "totals"],
    },
    {
        # Файл + метод: вузький scope, лише два очікуваних символи.
        "question": "Що робить compress() у Cart.ts?",
        "expected": ["compress", "Cart"],
    },
    {
        # Ланцюжок викликів через кілька модулів.
        "question": "Як виконується розміщення замовлення (submitOrder)?",
        "expected": ["useSubmitOrder", "useOrders"],
    },
]


_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)
_IMPORT_LINE_RE = re.compile(r"^\s*(import\s|from\s.*import|export\s+\{[^}]*\}\s+from)")


def _classify_block(code: str) -> str:
    """Чи містить код-блок реальне тіло, чи тільки імпорти/коментарі."""
    lines = [ln for ln in code.splitlines() if ln.strip()]
    if not lines:
        return "empty"
    code_lines = [ln for ln in lines if not _IMPORT_LINE_RE.match(ln)
                  and not ln.lstrip().startswith(("//", "#", "*", "/*"))]
    if len(code_lines) >= 3:
        return "body"
    if any(_IMPORT_LINE_RE.match(ln) for ln in lines):
        return "import_only"
    return "tiny"


def _verdict(case: CaseResult) -> str:
    """Heuristic: PASS якщо є body-блоки і ≥50% expected keywords matched."""
    kw_ratio = case.expected_keywords_hit / max(1, case.expected_keywords_total)
    if case.body_blocks >= 1 and kw_ratio >= 0.5 and case.files_read <= 30:
        return "PASS"
    if case.body_blocks >= 1 and kw_ratio >= 0.3:
        return "WEAK"
    return "FAIL"


def run_case(qa, case: dict, repo: str) -> CaseResult:
    t0 = time.time()
    ans = qa.ask(question=case["question"], repo=repo)
    elapsed = time.time() - t0

    blocks = _CODE_BLOCK_RE.findall(ans.text)
    classes = [_classify_block(b) for b in blocks]
    body = sum(1 for c in classes if c == "body")
    import_only = sum(1 for c in classes if c == "import_only")

    expected = case["expected"]
    hit = sum(1 for kw in expected if kw.lower() in ans.text.lower())

    res = CaseResult(
        question=case["question"],
        route=ans.route,
        qtype=ans.question_type,
        files_read=len(ans.files_read),
        code_blocks=len(blocks),
        import_only_blocks=import_only,
        body_blocks=body,
        expected_keywords_hit=hit,
        expected_keywords_total=len(expected),
        tokens_in=ans.tokens_in,
        tokens_out=ans.tokens_out,
        elapsed_s=round(elapsed, 1),
        verdict="",
    )
    res.verdict = _verdict(res)
    return res


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=os.environ.get("CELMIS_REAL_REPO"),
                   help="Repo slug to evaluate (default: $CELMIS_REAL_REPO)")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()
    if not args.repo:
        p.error("--repo is required (or set CELMIS_REAL_REPO)")

    import logging
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    for n in ("httpx", "httpcore", "google_genai", "qdrant_client"):
        logging.getLogger(n).setLevel(logging.ERROR)

    from src.qa.orchestrator import QAOrchestrator
    qa = QAOrchestrator()

    results: list[CaseResult] = []
    for case in CASES:
        try:
            r = run_case(qa, case, args.repo)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ ERROR on case: {case['question'][:60]} → {exc}", file=sys.stderr)
            continue
        results.append(r)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
    else:
        print(f"\n{'─' * 100}")
        print(f"{'verdict':<6} {'files':>5} {'blocks':>6} {'body':>4} {'imp':>4} "
              f"{'kw':>5} {'tok':>10} {'sec':>5}  question")
        print(f"{'─' * 100}")
        for r in results:
            kw = f"{r.expected_keywords_hit}/{r.expected_keywords_total}"
            tok = f"{r.tokens_in}→{r.tokens_out}"
            q = r.question[:55]
            print(f"{r.verdict:<6} {r.files_read:>5} {r.code_blocks:>6} "
                  f"{r.body_blocks:>4} {r.import_only_blocks:>4} {kw:>5} "
                  f"{tok:>10} {r.elapsed_s:>5}  {q}")

    pass_count = sum(1 for r in results if r.verdict == "PASS")
    print(f"\nPASS={pass_count}/{len(results)}", file=sys.stderr)
    return 0 if pass_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
