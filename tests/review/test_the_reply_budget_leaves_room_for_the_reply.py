"""Стеля відповіді агента мусить лишати місце після токенів думання.

4096 розраховували на модель, яка витрачає вихідний бюджет тільки на
відповідь. У Gemini 3.x у той самий бюджет рахуються токени думання, і вони
з'їдають його майже цілком — заміряно 572 reasoning на 613 вихідних.
Наслідок: architect у 20 з 28 викликів упирався в стелю, JSON приходив
обрізаним, і агент падав з "no JSON array in the reply".

Тест ловить два способи повернути цю поведінку: захардкоджений дефолт замість
налаштування, і саме налаштування, опущене назад до колишньої стелі.
"""

from __future__ import annotations

import inspect

from src.review.agents.base import LLMReviewAgent
from src.review.settings import get_review_settings


def test_the_budget_is_not_hardcoded_in_the_signature():
    """Дефолт має приходити з налаштувань, а не стояти числом у сигнатурі."""
    sig = inspect.signature(LLMReviewAgent._generate_and_parse)
    default = sig.parameters["max_output_tokens"].default
    assert default is None, (
        f"max_output_tokens за замовчуванням = {default!r}; має бути None, "
        f"щоб значення бралося з review-налаштувань, а не з сигнатури"
    )


def test_the_configured_ceiling_leaves_room_after_thinking():
    """Стеля має бути помітно вищою за обсяг, який з'їдає думання."""
    ceiling = get_review_settings().agent_max_output_tokens
    assert ceiling >= 8192, (
        f"agent_max_output_tokens={ceiling}: на цій стелі відповідь architect "
        f"обривається — заміряно середні 4316 вихідних токенів на виклик, "
        f"з яких переважна частина йде на reasoning"
    )


def test_the_ceiling_stays_inside_what_the_model_accepts():
    """І не вище за те, що моделі приймають — інакше провайдер відмовить."""
    ceiling = get_review_settings().agent_max_output_tokens
    assert ceiling <= 65536, (
        f"agent_max_output_tokens={ceiling} перевищує max_output_tokens "
        f"моделей Gemini 3.x (65 536)"
    )
