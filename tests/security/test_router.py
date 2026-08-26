"""Тести для query router'а — правильний path для різних типів питань."""

from __future__ import annotations

import pytest

from src.qa.router import QueryRouter, RoutePath


@pytest.fixture
def router() -> QueryRouter:
    return QueryRouter()


@pytest.mark.parametrize(
    "question",
    [
        "що робить `validateEmail`?",
        "show me the code of useAuth",
        "how does `LoginForm` work",
        "хто викликає `Database.query`?",
    ],
)
def test_exact_symbol_path(router: QueryRouter, question: str) -> None:
    d = router.route(question)
    assert d.path == RoutePath.EXACT_SYMBOL
    assert len(d.symbols) >= 1


@pytest.mark.parametrize(
    "question",
    [
        "explain src/auth/useAuth.ts",
        "що в файлі api/client.js",
        "поясни src/components/LoginForm.tsx",
    ],
)
def test_file_path_route(router: QueryRouter, question: str) -> None:
    d = router.route(question)
    assert d.path == RoutePath.FILE_PATH
    assert len(d.files) >= 1


@pytest.mark.parametrize(
    "question",
    [
        "як працює логін користувача?",
        "how does the order flow work end to end",
        "опиши процес реєстрації нового користувача",
        "розкажи про архітектуру системи аутентифікації",
    ],
)
def test_natural_language_route(router: QueryRouter, question: str) -> None:
    d = router.route(question)
    assert d.path == RoutePath.NATURAL_LANGUAGE
