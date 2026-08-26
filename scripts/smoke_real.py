"""End-to-end smoke runner — real Gemini, без mock'ів.

Quota-friendly: 3-5 коротких питань з обмеженим max_output_tokens.
Прогонає повний pipeline: router → Tier 2 graph → Tier 3 code → Gemini → markdown.
Зберігає answers у data/smoke/<timestamp>/ для рев'ю + audit log auto-rotated.

Запуск (repo обов'язковий — через прапорець або env):
    CELMIS_REAL_REPO=<repo-slug> python -m scripts.smoke_real
    python -m scripts.smoke_real --repo <repo-slug>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

# Configure logging BEFORE importing src.*: basicConfig() is a silent no-op
# once anything has attached a handler to the root logger, and records emitted
# while those modules import would otherwise fall through to
# logging.lastResort — unformatted, and without this WARNING threshold.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from src.config import get_settings  # noqa: E402 — must follow basicConfig, see above
from src.qa.orchestrator import QAOrchestrator  # noqa: E402 — must follow basicConfig

# Підбір питань щоб охопити різні шляхи + не палити quota.
# ПРИКЛАД набору: символи нижче взяті з демо-репозиторію (ordering/pricing).
# Перед прогоном проти власного репо підмініть питання на свої — важливо
# зберегти покриття шляхів (exact symbol / impact / natural language), а не текст.
TEST_QUESTIONS = [
    # Path A — exact symbol з backtick. Очікуємо technical answer з code blocks.
    "Як реалізована функція `useEstimate`? Стисло — у 3-5 реченнях.",
    # Path A — impact analysis (different question type)
    "Хто викликає `requestQuote`? Дай список найважливіших caller'ів.",
    # Path C — natural language без backtick. Стискаємо vault hits → graph fallback.
    "Як працює розрахунок вартості замовлення?",
]

# Limit щоб не палити багато токенів
MAX_OUTPUT_TOKENS = 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("CELMIS_REAL_REPO"),
                        help="Repo slug to smoke-test (default: $CELMIS_REAL_REPO)")
    parser.add_argument("--limit-tokens", type=int, default=MAX_OUTPUT_TOKENS)
    parser.add_argument("--questions", type=int, default=len(TEST_QUESTIONS),
                        help="Кількість питань (1..N)")
    args = parser.parse_args()
    if not args.repo:
        parser.error("--repo is required (or set CELMIS_REAL_REPO)")

    settings = get_settings()
    settings.gemini_max_output_tokens = args.limit_tokens

    repo = args.repo
    repo_path = settings.repo_path(repo)
    if not repo_path.exists():
        print(f"❌ Repo not cloned: {repo_path}")
        return 1

    db = settings.repo_graph_path(repo)
    if not db.exists():
        print(f"❌ Graph not indexed: {db}")
        print(f"Run: analyzer index {repo}")
        return 1

    # Output dir
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = settings.data_dir / "smoke" / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"REAL E2E SMOKE — repo={repo}")
    print("=" * 70)
    print(f"Generation model:  {settings.gemini_generation_model}")
    print(f"Embedding model:   {settings.gemini_embedding_model}")
    print(f"max_output_tokens: {args.limit_tokens}")
    print(f"Output dir:        {out_dir}")
    print()

    orch = QAOrchestrator()

    # Якщо vault порожній — vault search верне 0 hits, OK для демо.
    # Tier 2 graph виходить з seed-символів witha real data.

    questions = TEST_QUESTIONS[: args.questions]
    summary: list[dict] = []

    for i, q in enumerate(questions, 1):
        print(f"\n{'─' * 70}")
        print(f"[{i}/{len(questions)}] {q}")
        print("─" * 70)

        t0 = time.time()
        try:
            answer = orch.ask(question=q, repo=repo)
            elapsed = time.time() - t0
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - t0
            print(f"❌ Failed in {elapsed:.1f}s: {type(exc).__name__}: {str(exc)[:300]}")
            summary.append({
                "question": q, "error": str(exc)[:300], "elapsed_s": elapsed,
            })
            continue

        print(f"\n→ route={answer.route}  type={answer.question_type}  "
              f"vault={len(answer.vault_hits)}  files={len(answer.files_read)}  "
              f"tokens={answer.tokens_in}/{answer.tokens_out}  {elapsed:.1f}s")
        if answer.files_read:
            print("\nFiles read (top 5):")
            for f in answer.files_read[:5]:
                print(f"  • {f}")

        # Зберігаємо повний answer у MD файл
        slug = f"{i:02d}_{_slugify(q)[:40]}"
        answer_md = out_dir / f"{slug}.md"
        answer_md.write_text(
            f"# Question\n\n> {q}\n\n"
            f"# Answer\n\n{answer.text}\n\n"
            f"---\n\n"
            f"**route:** {answer.route} · "
            f"**type:** {answer.question_type} · "
            f"**files:** {len(answer.files_read)} · "
            f"**tokens:** in={answer.tokens_in} out={answer.tokens_out} · "
            f"**time:** {elapsed:.1f}s\n\n"
            f"## Files read\n\n"
            + "\n".join(f"- `{f}`" for f in answer.files_read)
        )
        print(f"  → saved {answer_md.relative_to(settings.workspace_dir)}")

        # Console preview answer (truncated)
        preview = answer.text[:500] + ("…" if len(answer.text) > 500 else "")
        print(f"\n--- preview ---\n{preview}\n--- end ---")

        summary.append({
            "question": q,
            "route": answer.route,
            "type": answer.question_type,
            "tokens_in": answer.tokens_in,
            "tokens_out": answer.tokens_out,
            "files": answer.files_read,
            "elapsed_s": round(elapsed, 1),
            "answer_path": str(answer_md.relative_to(settings.workspace_dir)),
        })

    # Final summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total_in = sum(s.get("tokens_in", 0) for s in summary)
    total_out = sum(s.get("tokens_out", 0) for s in summary)
    total_time = sum(s.get("elapsed_s", 0) for s in summary)
    errors = sum(1 for s in summary if s.get("error"))
    print(f"  Questions:    {len(summary)}")
    print(f"  Errors:       {errors}")
    print(f"  Total tokens: {total_in:_} in / {total_out:_} out")
    print(f"  Total time:   {total_time:.1f}s")
    print(f"  Output:       {out_dir}")
    print(f"  Audit log:    {settings.audit_log_path}")

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"  Summary:      {summary_path.relative_to(settings.workspace_dir)}")

    return 0 if errors == 0 else 2


def _slugify(text: str) -> str:
    import re
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[-\s_]+", "-", s).strip("-")
    return s or "q"


if __name__ == "__main__":
    sys.exit(main())
