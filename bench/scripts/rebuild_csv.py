#!/usr/bin/env python3
"""Перезбір results-run-a.csv із логів рев'ю.

Перша версія парсера брала `findings=(\\d+)` першим збігом — а першим у логу
йде `structural_done … findings=0`, лічильник ОДНОГО агента. Підсумок лежить
у рядку `review_complete`, і читати треба саме його: інакше кожен PR
записується як нуль знахідок, і весь прогін виглядає порожнім.
"""
from __future__ import annotations
import csv, pathlib, re, sys

W = pathlib.Path.home() / "Desktop/celmis-bench-work"
LOGS = sorted((W / "logs").glob("review-*.log")) + \
       sorted((pathlib.Path.home() / "Desktop/Celmis/bench/results").glob("review-fork-*.log"))
OUT = W / "results-run-a.csv"

FIELDS = ["fork", "pr", "status", "findings", "verdict", "agents_run",
          "agents_failed", "tokens_in", "tokens_out", "seconds", "log"]

# Одне джерело істини на весь рядок — підсумковий запис оркестратора.
COMPLETE = re.compile(
    r"review_complete pr=(?P<pr>\d+) status=(?P<status>\w+) "
    r"findings=(?P<findings>\d+) verdict=(?P<verdict>\w+) "
    r"elapsed=(?P<elapsed>[\d.]+)s agents_run=(?P<run>[\w,\-]+) "
    r"agents_failed=(?P<failed>[\w,\-]+)"
)


def fork_of(log: pathlib.Path, text: str) -> str:
    m = re.search(r"Reviewing.*?github:([\w.\-]+/[\w.\-]+)#", text)
    if m:
        return m.group(1)
    stem = log.stem.replace("review-", "")
    return stem.rsplit("__", 1)[0].replace("__", "/", 1)


def main() -> int:
    rows, skipped = [], []
    for log in LOGS:
        text = log.read_text(errors="replace")
        m = COMPLETE.search(text)
        if not m:
            skipped.append(log.name)
            continue
        rows.append({
            "fork": fork_of(log, text), "pr": m["pr"], "status": m["status"],
            "findings": m["findings"], "verdict": m["verdict"],
            "agents_run": m["run"].replace(",", "|"),
            "agents_failed": m["failed"].replace(",", "|"),
            "tokens_in": (re.search(r"tokens:\s+([\d,]+)/", text) or ["", "0"])[1].replace(",", ""),
            "tokens_out": (re.search(r"tokens:\s+[\d,]+/([\d,]+)", text) or ["", "0"])[1].replace(",", ""),
            "seconds": m["elapsed"], "log": log.name,
        })

    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: (r["fork"], int(r["pr"]))):
        k = (r["fork"], r["pr"])
        if k not in seen:
            seen.add(k); uniq.append(r)

    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader(); w.writerows(uniq)

    tot = sum(int(r["findings"]) for r in uniq)
    fail = sum(1 for r in uniq if r["agents_failed"] != "-")
    print(f"  рядків: {len(uniq)}   знахідок разом: {tot}   з падінням агента: {fail}")
    if skipped:
        print(f"  без review_complete (не дорахованих): {len(skipped)}")
    print(f"  → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
