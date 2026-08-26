"""Прогон 3 питань через QA orchestrator + dump-у відповіді у JSON для порівняння."""

from __future__ import annotations

import json
import logging
import os
import sys
import time

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


REPO_ENV = "CELMIS_REAL_REPO"


def _repo() -> str:
    """Repo slug береться з $CELMIS_REAL_REPO — дефолту в коді немає навмисно."""
    repo = os.environ.get(REPO_ENV)
    if not repo:
        raise SystemExit(f"{REPO_ENV} is not set — export it with the repo slug to query.")
    return repo


def main() -> int:
    repo = _repo()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    for n in ("httpx", "httpcore", "google_genai", "qdrant_client"):
        logging.getLogger(n).setLevel(logging.ERROR)

    from src.qa.orchestrator import QAOrchestrator
    qa = QAOrchestrator()

    out: list[dict] = []
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n=== Q{i}: {q[:60]}... ===", file=sys.stderr)
        t0 = time.time()
        try:
            ans = qa.ask(question=q, repo=repo)
            elapsed = time.time() - t0
            out.append({
                "id": i,
                "question": q,
                "route": ans.route,
                "qtype": ans.question_type,
                "files_read": ans.files_read,
                "files_count": len(ans.files_read),
                "tokens_in": ans.tokens_in,
                "tokens_out": ans.tokens_out,
                "elapsed_s": round(elapsed, 1),
                "text": ans.text,
            })
            print(f"  files={len(ans.files_read)} tokens={ans.tokens_in}->{ans.tokens_out} {elapsed:.1f}s",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {exc}", file=sys.stderr)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
