"""Three guards around `show_in_ui` that do not depend on the model behaving.

1. A planner that files "where do I paste the GitLab token" under `help` is
   corrected in code: the plan gains a `show_in_ui` step. Questions that
   merely ask for an explanation, and sentences that name two places at once,
   are left exactly as the planner made them.
2. When a plan carries both a `help` answer and a `show_in_ui` that could not
   open (ambiguous repository, no permission), the explanation of the failure
   is added to the help text and does not replace it.
3. The popovers are in the language of the question. A sentence the model
   wrote in the wrong script is replaced by the catalogue's English one.
"""

from __future__ import annotations

import pytest

from src.automation import ui_catalogue as cat
from src.automation.chat import Plan, Step, add_ui_safety_net
from src.sync.handlers import _join_explained

# ── 1. the safety net ────────────────────────────────────────────────────


@pytest.mark.parametrize("question,dest,focus0", [
    ("куди вставити токен GitLab для репозиторію?",
     "connections", "connection_provider:gitlab"),
    ("where do I paste the GitHub token", "connections",
     "connection_provider:github"),
    ("де вказати файли, які треба ігнорувати?",
     "review_settings.filters", "ignore_globs"),
    ("как настроить игнорирование lock-файлов", "review_settings.filters",
     "ignore_globs"),
    ("where can I set the severity threshold", "review_settings.filters",
     "comment_min_severity"),
    ("I need to add custom prompts for the reviewer",
     "review_settings.prompts", "base_instruction"),
    ("куда добавить ключ API провайдера", "llm", "llm_provider_keys"),
    ("як налаштувати сповіщення в Slack", "notifications",
     "notifications_channel"),
])
def test_a_setup_question_is_mapped_to_its_page(question, dest, focus0):
    got = cat.suggest_destination(question)
    assert got is not None
    assert got["destination"] == dest
    assert got["focus"][0] == focus0


@pytest.mark.parametrize("question", [
    "what is Celmis?",
    "що таке верифікатор?",
    "почему ревью игнорирует файлы",
    "explain how the severity threshold works",
    "why does the reviewer skip drafts",
    "hello",
    "",
    # a place is named but nothing asks where or how to set it
    "the GitLab token expired yesterday",
    # two different pages are equally good answers: no guess
    "where do I set the verifier and the severity",
    # one weak word is not enough for the connection page
    "where is the token limit documented",
])
def test_an_explanation_or_an_unclear_question_is_left_to_the_planner(question):
    assert cat.suggest_destination(question) is None


def _help_plan(*actions):
    return Plan(note="Open Settings, then Connections.",
                steps=[Step(action=a) for a in actions])


def test_a_help_only_plan_for_a_setup_question_gains_the_spotlight():
    plan = add_ui_safety_net(_help_plan("help"),
                             "куди вставити токен GitLab")
    assert [s.action for s in plan.steps] == ["help", "show_in_ui"]
    assert plan.steps[1].arguments["destination"] == "connections"
    assert plan.note == "Open Settings, then Connections."


def test_a_plan_that_already_has_the_spotlight_is_not_doubled():
    plan = add_ui_safety_net(_help_plan("help", "show_in_ui"),
                             "куди вставити токен GitLab")
    assert [s.action for s in plan.steps] == ["help", "show_in_ui"]


def test_a_plan_with_a_write_in_it_is_never_touched():
    plan = add_ui_safety_net(_help_plan("help", "update_review_setting"),
                             "куди вставити токен GitLab")
    assert [s.action for s in plan.steps] == ["help", "update_review_setting"]


def test_a_plan_without_help_is_never_touched():
    plan = add_ui_safety_net(_help_plan("list_repos"),
                             "куди вставити токен GitLab")
    assert [s.action for s in plan.steps] == ["list_repos"]


def test_an_explanation_question_keeps_its_help_only_plan():
    plan = add_ui_safety_net(_help_plan("help"), "what is Celmis?")
    assert [s.action for s in plan.steps] == ["help"]


# ── 2. the help text survives a spotlight that could not open ─────────────


def test_the_help_text_and_the_reason_are_both_kept():
    steps = [{"action": "help"}, {"action": "show_in_ui"}]
    out = _join_explained("Steps: open Settings.", "Which repo: shop-api or shop-web?",
                          steps)
    assert out.startswith("Steps: open Settings.")
    assert out.endswith("Which repo: shop-api or shop-web?")


def test_without_a_help_answer_the_explanation_still_replaces_the_note():
    out = _join_explained("Opening it now.", "Which repo?",
                          [{"action": "show_in_ui"}])
    assert out == "Which repo?"


def test_an_empty_explanation_leaves_the_note_alone():
    assert _join_explained("Steps.", "", [{"action": "help"}]) == "Steps."


# ── 3. the popovers speak the question's language ─────────────────────────


@pytest.mark.parametrize("text,lang", [
    ("Welcome to the prompt box", "en"),
    ("Впишіть правила для рев'юера", "uk"),
    ("Это нужно записать здесь", "ru"),
    ("Додай інструкцію", "uk"),
    ("Правила", "cyr"),
    ("", ""),
    ("12345", ""),
])
def test_the_script_check_names_the_language(text, lang):
    assert cat.detect_language(text) == lang


@pytest.mark.parametrize("text,asked,ok", [
    ("Впишіть правила", "uk", True),
    ("Write the rules", "uk", False),
    ("Впишите правила, это нужно", "uk", False),
    ("Впишіть правила, це важливо", "ru", False),
    ("Впишите правила", "ru", True),
    ("Правила", "uk", True),           # no telling letter: accepted
    ("Впишіть правила", "en", False),
    ("Write the rules", "en", True),
    ("Escribe las reglas", "es", True),  # a language it cannot judge
])
def test_a_text_is_accepted_only_in_the_asked_language(text, asked, ok):
    assert cat.text_in_language(text, asked) is ok


def _action(text, language):
    return cat.build_ui_action(
        destination="review_settings.filters", repo=None,
        focus=[{"field": "ignore_globs", "text": text}],
        repos=[], caller={"role": "admin"}, language=language)


def test_a_popover_in_the_right_language_is_kept():
    out = _action("Додайте шаблони файлів, які пропускати", "uk")
    assert out["steps"][0]["text"] == "Додайте шаблони файлів, які пропускати"


def test_a_popover_in_the_wrong_language_falls_back_to_the_catalogue():
    out = _action("Add the file patterns to skip", "uk")
    assert out["steps"][0]["text"] == cat._tidy(cat.FIELDS_BY_KEY["ignore_globs"].what)


def test_a_russian_popover_for_a_ukrainian_question_is_replaced():
    out = _action("Добавьте шаблоны файлов", "uk")
    assert out["steps"][0]["text"].startswith("Files matching")


def test_without_a_known_language_the_text_is_kept():
    out = _action("Add the file patterns to skip", "")
    assert out["steps"][0]["text"] == "Add the file patterns to skip"


# ── 4. the net carries the repository the question names ─────────────────

REPOS = [
    {"repo": "acme-shop-api", "full_name": "acme/shop-api"},
    {"repo": "acme-shop-web", "full_name": "acme/shop-web"},
    {"repo": "acme-billing", "full_name": "acme/billing"},
    {"repo": "acme-docs", "full_name": "acme/docs"},
]


@pytest.mark.parametrize("question,slug", [
    ("де вказати файли, які ігнорувати, у репозиторії billing?", "acme-billing"),
    ("where do I add custom prompts for acme/docs", "acme-docs"),
    ("where do I add custom prompts for acme-shop-api", "acme-shop-api"),
    ("where do I add custom prompts for the biling repo", "acme-billing"),
])
def test_the_repository_named_in_the_question_is_carried(question, slug):
    plan = add_ui_safety_net(_help_plan("help"), question, REPOS)
    assert plan.steps[-1].arguments["repo"] == slug


def test_no_repository_is_invented_when_none_is_named():
    plan = add_ui_safety_net(
        _help_plan("help"), "where do I add custom prompts for the reviewer",
        REPOS)
    assert plan.steps[-1].action == "show_in_ui"
    assert "repo" not in plan.steps[-1].arguments


def test_no_repository_without_a_list_to_match_against():
    plan = add_ui_safety_net(_help_plan("help"),
                             "where do I add custom prompts for acme/docs")
    assert "repo" not in plan.steps[-1].arguments


def test_an_ambiguous_name_is_passed_as_typed_so_the_agent_asks():
    plan = add_ui_safety_net(
        _help_plan("help"), "where do I add custom prompts for the shop repo",
        REPOS)
    arg = plan.steps[-1].arguments["repo"]
    assert arg == "shop"
    out = cat.build_ui_action(destination="review_settings.prompts", repo=arg,
                              repos=REPOS, caller={"role": "admin"})
    assert out["error"] == "repo_ambiguous"


def test_two_different_repositories_in_one_sentence_are_not_guessed():
    plan = add_ui_safety_net(
        _help_plan("help"),
        "where do I add custom prompts for billing and docs", REPOS)
    assert "repo" not in plan.steps[-1].arguments
