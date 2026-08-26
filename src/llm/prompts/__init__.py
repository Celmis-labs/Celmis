"""Prompts for various operations — generation and Q&A."""

from src.llm.prompts.ba_answer import BA_ANSWER_PROMPT, BA_ANSWER_SYSTEM
from src.llm.prompts.feature import FEATURE_PROMPT, FEATURE_SYSTEM
from src.llm.prompts.integration import INTEGRATION_PROMPT, INTEGRATION_SYSTEM
from src.llm.prompts.module_prd import MODULE_PRD_PROMPT, MODULE_PRD_SYSTEM
from src.llm.prompts.overview import OVERVIEW_PROMPT, OVERVIEW_SYSTEM
from src.llm.prompts.technical_answer import TECHNICAL_ANSWER_PROMPT, TECHNICAL_ANSWER_SYSTEM

__all__ = [
    "MODULE_PRD_PROMPT",
    "MODULE_PRD_SYSTEM",
    "FEATURE_PROMPT",
    "FEATURE_SYSTEM",
    "INTEGRATION_PROMPT",
    "INTEGRATION_SYSTEM",
    "OVERVIEW_PROMPT",
    "OVERVIEW_SYSTEM",
    "TECHNICAL_ANSWER_PROMPT",
    "TECHNICAL_ANSWER_SYSTEM",
    "BA_ANSWER_PROMPT",
    "BA_ANSWER_SYSTEM",
]
