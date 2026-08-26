"""Температура агента приходить із налаштувань, а не з константи в коді.

Вона стояла літералом 0.1 у двох місцях `LLMReviewAgent`. Поки всі агенти
працювали на одній родині моделей, це не заважало. Щойно один агент вказали на
claude-sonnet-5, виявилось, що модель приймає ЛИШЕ temperature=1 і 400-ить на
0.1 — а змінити число не було де, крім як у коді.

Тести ловлять обидва способи повернути це: літерал у виклику й налаштування,
опущене поза розумний діапазон.
"""

from __future__ import annotations

import inspect

import pytest

from src.review.agents import base as agent_base
from src.review.settings import get_review_settings, resolve_agent_llm


def test_no_literal_temperature_in_the_agent_call():
    """У виклику LLM не має бути числа — лише посилання на розв'язані налаштування."""
    src = inspect.getsource(agent_base)
    stripped = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "temperature=0.1" not in stripped, (
        "температура повернулась літералом — модель, яка не приймає це значення, "
        "знову не матиме де його змінити"
    )


def test_the_default_is_low_enough_to_be_deterministic():
    t = get_review_settings().agent_temperature
    assert 0.0 <= t <= 0.5, (
        f"agent_temperature={t}: рев'юер, який на той самий діф відповідає "
        f"щоразу інакше, не рев'юер"
    )


def test_a_per_agent_override_wins_over_the_default():
    default = get_review_settings().agent_temperature
    other = 0.0 if default != 0.0 else 0.4
    r = resolve_agent_llm("architect", policy={"agents": {"architect": {"temperature": other}}})
    assert r.temperature == pytest.approx(other), (
        "перевизначення для агента не дійшло — ланцюжок успадкування розірваний"
    )


def test_zero_is_a_value_and_not_an_absence():
    """0.0 — найдетермінованіше значення, і воно не має читатись як «не задано»."""
    r = resolve_agent_llm("architect", policy={"agents": {"architect": {"temperature": 0.0}}})
    assert r.temperature == pytest.approx(0.0), (
        "нуль зник — ланцюг пройдено через `or`, а не через явну перевірку на None"
    )
