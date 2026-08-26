#!/usr/bin/env python3
"""Відтворення бенчмарк-PR через GitHub API — без жодного локального клону.

Оригінальний step0 клонує репозиторій і пушить його в новий: 50 PR над сімома
джерельними репо = 57.9 GB вивантаження, бо GitHub не ділить об'єкти між
НЕспорідненими репозиторіями.

Форк — ділить. Усередині мережі форків об'єкти спільні, тож гілку можна
створити прямо на SHA з батьківського репозиторію, не маючи його локально.
Перевірено: діф отриманого PR побайтово дорівнює оригінальному.

Ціна — одне обмеження GitHub: один форк репозиторію на організацію. Тому
50 PR лягають у 7 форків по 10, а не в 50 репозиторіїв. Розкладку знає
mapping.json, який пише цей скрипт; підрахунок від імен репозиторіїв не
залежить (див. структуру reviews[] у benchmark_data.json).
"""
from __future__ import annotations
import json, os, sys, time, pathlib, glob, urllib.request, urllib.error

ORG = "celmis-bench"
TOKEN = os.environ["GITHUB_TOKEN"]
OUT = pathlib.Path.home() / "Desktop/celmis-bench-work/mapping.json"


def api(method: str, path: str, body: dict | None = None, accept: str | None = None):
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {TOKEN}")
    req.add_header("Accept", accept or "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw and not accept else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw[:200].decode(errors="replace")}


def find_fork(source: str) -> str | None:
    """Наявний у нашій організації форк саме цього джерела."""
    page = 1
    while True:
        st, items = api("GET", f"/orgs/{ORG}/repos?per_page=100&page={page}&type=all")
        if st != 200 or not items:
            return None
        for it in items:
            if not it.get("fork"):
                continue
            st2, full = api("GET", f"/repos/{ORG}/{it['name']}")
            if st2 == 200 and (full.get("parent") or {}).get("full_name") == source:
                return it["name"]
        page += 1


def canonical(source: str) -> str:
    """Справжнє ім'я репозиторію. calcom/cal.com перейменовано на calcom/cal.diy,
    і GitHub віддає 301 — GET по редіректу веде до канонічного, POST /forks ні."""
    st, d = api("GET", f"/repos/{source}")
    if st == 301 and d.get("url"):
        st2, d2 = api("GET", d["url"])
        if st2 == 200:
            return d2["full_name"]
    return d.get("full_name", source) if st == 200 else source


def ensure_fork(source: str) -> str:
    source = canonical(source)
    existing = find_fork(source)
    if existing:
        print(f"  форк {source} → {ORG}/{existing} (вже є)")
        return existing
    st, d = api("POST", f"/repos/{source}/forks", {"organization": ORG})
    if st not in (200, 201, 202):
        raise RuntimeError(f"fork {source}: {d.get('message')}")
    name = d["name"]
    print(f"  форк {source} → {ORG}/{name} (створено)")
    for _ in range(30):                      # форк асинхронний
        time.sleep(4)
        if api("GET", f"/repos/{ORG}/{name}")[0] == 200:
            break
    api("PUT", f"/repos/{ORG}/{name}/actions/permissions", {"enabled": False})
    api("DELETE", f"/repos/{ORG}/{name}/vulnerability-alerts")
    api("DELETE", f"/repos/{ORG}/{name}/automated-security-fixes")
    return name


def ensure_ref(fork: str, ref: str, sha: str) -> None:
    st, d = api("POST", f"/repos/{ORG}/{fork}/git/refs",
                {"ref": f"refs/heads/{ref}", "sha": sha})
    if st in (200, 201):
        return
    if "already exists" in str(d.get("message", "")):
        api("PATCH", f"/repos/{ORG}/{fork}/git/refs/heads/{ref}",
            {"sha": sha, "force": True})
        return
    raise RuntimeError(f"ref {ref}: {d.get('message')}")


def main() -> int:
    mapping = json.loads(OUT.read_text()) if OUT.exists() else {}
    entries = []
    for f in sorted(glob.glob("golden_comments/*.json")):
        for e in json.load(open(f)):
            entries.append((os.path.basename(f)[:-5], e))

    print(f"PR у роботі: {len(entries)}  вже зроблено: {len(mapping)}\n")
    forks: dict[str, str] = {}
    done = fail = 0
    for i, (bucket, e) in enumerate(entries, 1):
        url = e["url"]
        if url in mapping:
            done += 1
            continue
        parts = url.split("/")
        source, num = f"{parts[3]}/{parts[4]}", int(parts[6])
        print(f"[{i}/{len(entries)}] {source}#{num}")
        try:
            if source not in forks:
                forks[source] = ensure_fork(source)
            fork = forks[source]

            st, pr = api("GET", f"/repos/{source}/pulls/{num}")
            if st != 200:
                raise RuntimeError(f"PR: {pr.get('message')}")
            base_ref, pr_ref = f"bench-base-{num}", f"bench-pr-{num}"
            ensure_ref(fork, base_ref, pr["base"]["sha"])
            ensure_ref(fork, pr_ref, pr["head"]["sha"])

            st, new = api("POST", f"/repos/{ORG}/{fork}/pulls", {
                "title": pr["title"], "head": pr_ref, "base": base_ref,
                "body": f"Benchmark reproduction of {url}",
            })
            if st not in (200, 201):
                msg = str(new.get("message", "")) + str(new.get("errors", ""))
                if "already exists" in msg:
                    st2, lst = api(
                        "GET", f"/repos/{ORG}/{fork}/pulls?head={ORG}:{pr_ref}&state=all")
                    if st2 == 200 and lst:
                        new = lst[0]
                    else:
                        raise RuntimeError(f"PR exists but not found: {msg}")
                else:
                    raise RuntimeError(f"create PR: {msg}")

            mapping[url] = {
                "bucket": bucket, "source": source, "source_pr": num,
                "fork": f"{ORG}/{fork}", "pr_number": new["number"],
                "pr_url": new["html_url"],
            }
            OUT.write_text(json.dumps(mapping, indent=2))
            print(f"     ✓ {new['html_url']}")
            done += 1
        except Exception as exc:                       # noqa: BLE001
            print(f"     ✗ {exc}")
            fail += 1
    print(f"\nготово: {done}  помилок: {fail}  mapping → {OUT}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
