"""A/B test двох subagent моделей на тих самих 3 питаннях.

Запускає кожну модель проти репозиторію з $CELMIS_REAL_REPO, фіксує:
- turns / tool_calls / selected / bodies_read
- input/output tokens (subagent + final synthesis)
- elapsed time
- bundle файлові набори
- subagent termination reason

Output: JSON з paired результатами + summary table.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass

QUESTIONS = [
    # An evaluation set is repository-specific by nature. These are shaped
    # like the questions this system is for — "how does X get built", "who
    # calls Y" — and are meant to be replaced with ones about whatever
    # CELMIS_REAL_REPO points at. They used to be about one customer's
    # product, which made the numbers meaningless anywhere else.
    "Describe how the data for the main request is assembled.",
    "Which functions trigger a recalculation?",
    "In what order is the main object constructed?",
]

MODELS = [
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
]


REPO_ENV = "CELMIS_REAL_REPO"


def _repo() -> str:
    """Repo slug береться з $CELMIS_REAL_REPO — дефолту в коді немає навмисно."""
    repo = os.environ.get(REPO_ENV)
    if not repo:
        raise SystemExit(f"{REPO_ENV} is not set — export it with the repo slug to query.")
    return repo


@dataclass
class RunResult:
    model: str
    question_id: int
    question: str
    files_count: int
    files: list[str]
    tokens_in: int
    tokens_out: int
    elapsed_s: float
    answer_text_head: str  # перші 600 чарів


def main() -> int:
    repo = _repo()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    for n in ("httpx", "httpcore", "google_genai", "qdrant_client"):
        logging.getLogger(n).setLevel(logging.ERROR)

    from src.config import get_settings
    from src.qa.orchestrator import QAOrchestrator

    settings = get_settings()
    results: list[RunResult] = []

    for model in MODELS:
        # Override subagent model для цього прогону
        settings.gemini_subagent_model = model
        print(f"\n{'='*70}\n=== MODEL: {model}\n{'='*70}", file=sys.stderr)

        # Свіжий orchestrator на кожен прогон щоб скинути будь-який client кеш
        qa = QAOrchestrator(settings=settings)

        for i, q in enumerate(QUESTIONS, 1):
            print(f"  Q{i}: {q[:55]}...", file=sys.stderr)
            t0 = time.time()
            try:
                ans = qa.ask(question=q, repo=repo)
                elapsed = time.time() - t0
                results.append(RunResult(
                    model=model, question_id=i, question=q,
                    files_count=len(ans.files_read),
                    files=ans.files_read,
                    tokens_in=ans.tokens_in,
                    tokens_out=ans.tokens_out,
                    elapsed_s=round(elapsed, 1),
                    answer_text_head=ans.text[:600],
                ))
                print(f"    files={len(ans.files_read)} tokens={ans.tokens_in}->{ans.tokens_out} {elapsed:.1f}s",
                      file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"    ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)

    # JSON dump
    print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))

    # Summary
    print(f"\n{'='*90}\nSUMMARY\n{'='*90}", file=sys.stderr)
    print(f"{'model':<35} {'Q':>2} {'files':>5} {'tok_in':>7} {'tok_out':>7} {'sec':>5}",
          file=sys.stderr)
    for r in results:
        print(f"{r.model:<35} {r.question_id:>2} {r.files_count:>5} "
              f"{r.tokens_in:>7} {r.tokens_out:>7} {r.elapsed_s:>5.1f}",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
