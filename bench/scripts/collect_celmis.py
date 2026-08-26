#!/usr/bin/env python3
"""Збір коментарів Celmis у структуру benchmark_data.json — заміна step1.

step1 знаходить прогони інструмента, парсячи ІМЕНА репозиторіїв
(`{repo}__{tool}__PR{n}__{date}`). Наша розкладка інша: 50 PR лежать у 7
форках, бо GitHub дозволяє один форк репозиторію на організацію. Пошук за
іменами тут не працює — розкладку знає mapping.json.

Змінюється рівно пошук. Структура, яку зчитують кроки 2 → 2.5 → 3 і суддя,
відтворюється точно: {tool, repo_name, pr_url, review_comments[]}. Тому
підрахунок лишається тим самим кодом на тих самих даних.
"""
from __future__ import annotations
import json, os, pathlib, sys, time, urllib.request, urllib.error

TOKEN = os.environ["GITHUB_TOKEN"]
TOOL = os.environ.get("CELMIS_TOOL_NAME", "celmis")
MAPPING = pathlib.Path.home() / "Desktop/celmis-bench-work/mapping.json"
DATA = pathlib.Path("results/benchmark_data.json")
BOT = os.environ.get("CELMIS_BOT_LOGIN", "celmis-bot")


def api(path: str):
    """GET із повагою до ліміту: 403/429 з x-ratelimit-remaining=0 — чекаємо."""
    url = f"https://api.github.com{path}"
    for _ in range(6):
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"token {TOKEN}")
        req.add_header("Accept", "application/vnd.github+json")
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
                wait = max(5, int(e.headers.get("x-ratelimit-reset", 0)) - int(time.time()) + 5)
                print(f"    ліміт вичерпано — чекаю {wait//60} хв")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"не вдалось: {path}")


def comments_for(repo: str, number: int) -> list[dict]:
    """Інлайн + огляди + issue-коментарі, лише від нашого бота.

    Три джерела, бо інструмент може сказати те саме в підсумку й у рядку;
    крок 2.5 їх дедуплікує сам, і подвійність тут не карається.
    """
    out: list[dict] = []
    for c in api(f"/repos/{repo}/pulls/{number}/comments"):
        if c["user"]["login"] == BOT:
            out.append({"path": c.get("path"), "line": c.get("line") or c.get("original_line"),
                        "body": c.get("body", ""), "kind": "inline"})
    for r in api(f"/repos/{repo}/pulls/{number}/reviews"):
        if r["user"]["login"] == BOT and (r.get("body") or "").strip():
            out.append({"path": None, "line": None, "body": r["body"], "kind": "review"})
    for c in api(f"/repos/{repo}/issues/{number}/comments"):
        if c["user"]["login"] == BOT:
            out.append({"path": None, "line": None, "body": c.get("body", ""), "kind": "issue"})
    return out


def main() -> int:
    mapping = json.loads(MAPPING.read_text())
    data = json.loads(DATA.read_text())
    added = missing = empty = 0

    for golden_url, m in mapping.items():
        if golden_url not in data:
            print(f"  ! немає золотого запису: {golden_url}")
            missing += 1
            continue
        cs = comments_for(m["fork"], m["pr_number"])
        if not cs:
            empty += 1
        entry = {"tool": TOOL, "repo_name": m["fork"].split("/")[-1],
                 "pr_url": m["pr_url"], "review_comments": cs}
        reviews = data[golden_url].setdefault("reviews", [])
        # Перезаписуємо свій запис, щоб повторний збір не подвоював коментарі.
        reviews[:] = [r for r in reviews if r.get("tool") != TOOL] + [entry]
        added += 1
        print(f"  {m['fork']}#{m['pr_number']:<5} коментарів: {len(cs)}")

    DATA.write_text(json.dumps(data, indent=2))
    print(f"\n  записано: {added}   без коментарів: {empty}   без золотого: {missing}")
    print(f"  → {DATA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
