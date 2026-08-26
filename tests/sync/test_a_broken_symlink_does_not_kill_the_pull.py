"""Битий симлінк у чекауті не має валити pull — а отже й індекс.

cal.com комітить `packages/prisma/.env -> ../../.env`; ціль у .gitignore, тож
у кожному свіжому клоні це битий симлінк. `_chmod_writable` перед pull робив
`Path.chmod` по всіх файлах з os.walk; chmod іде за посиланням — і на битому
симлінку це FileNotFoundError. Наслідок на бенчмарку: індекс cal.diy впав
шість разів поспіль і помер, 10 PR із 50 лишились без графу, а текст помилки
вказував на індексер, хоч падав клон.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from src.sync.clone import _chmod_readonly, _chmod_writable


def _checkout_with_broken_link(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "packages" / "prisma").mkdir(parents=True)
    (repo / "packages" / "prisma" / "schema.prisma").write_text("model X {}\n")
    (repo / ".git").mkdir()
    # ціль навмисно не створюється — рівно як у cal.com
    os.symlink("../../.env", repo / "packages" / "prisma" / ".env")
    assert (repo / "packages" / "prisma" / ".env").is_symlink()
    assert not (repo / "packages" / "prisma" / ".env").exists()
    return repo


def test_writable_skips_the_broken_symlink_and_still_fixes_real_files(tmp_path):
    repo = _checkout_with_broken_link(tmp_path)
    real = repo / "packages" / "prisma" / "schema.prisma"
    real.chmod(stat.S_IRUSR)

    _chmod_writable(repo)          # раніше: FileNotFoundError

    assert real.stat().st_mode & stat.S_IWUSR, "справжній файл так і не став writable"


def test_readonly_skips_the_broken_symlink_too(tmp_path):
    repo = _checkout_with_broken_link(tmp_path)
    _chmod_readonly(repo)          # симетрична функція, та сама пастка
    real = repo / "packages" / "prisma" / "schema.prisma"
    assert not (real.stat().st_mode & stat.S_IWUSR)
