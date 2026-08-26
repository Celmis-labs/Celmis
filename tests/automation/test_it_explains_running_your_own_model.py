"""«Чи можна свою модель?» is a question about the product, and the agent
answers questions about the product for free.

THE THIRD TOPIC. `explain` already answered "what is Celmis" and "what can you
do" out of prose written down in sixteen languages, costing nothing. "How do I
connect a local model?" landed in the same place every unrecognised sentence
lands — "I could not read that as an action" — which is a confident no to a
question the product has a yes for.

WHAT THE PLANNER SEES IS ALL THE PLANNER KNOWS. It is a small model, and the
only thing it is ever handed is the catalogue summaries. So the `self_hosted`
clause is written in the words people type — Ollama, vLLM, LM Studio, local
model, own hardware, on-prem, air-gapped — rather than in the words the
feature is filed under. The stand-in planner below is deliberately LEXICAL: it
scores the real prompt against the real sentence and can only match words it
was shown, so it fails the moment that clause stops naming them. What it
cannot do is the bridge a real model does — "свою модель" has no Latin anchor
at all — which is exactly why the vocabulary test exists next to it.

AND HALF THE PEOPLE ASKING NAME NO PRODUCT. Somebody who has already picked
Ollama says so; somebody who has not asks about the property instead — can it
run offline, is there an on-premise option, does it work without the cloud,
does our code leave our network. Those questions used to reach nothing, and
"can this run offline?" reached the AUDIT verb, which owns the word "run" by
way of "branch to read for this run only". That is the reachable-vocabulary
gap the cases below pin down, one sentence per family.

WHAT THIS FILE CAN AND CANNOT PROVE. It proves the clause NAMES those words
and, on a bag-of-words scorer, outscores its neighbours in the catalogue. It
does not prove a real small model picks the topic for any of these sentences:
that depends on a model reading prose, which no assertion here exercises.

THE COMMANDS ARE NOT PROSE, AND THEY ARE NOT HERE. `ollama serve`, the
EMBEDDING_* lines and the base URLs are English, backend-owned and already
served by GET /api/llm/local-setup-guide, which this client already fetches
and renders on the settings page. A copy in the reply would be a second place
to change them AND a frozen one: a run's result is written to its row, so
answers given last month would keep last month's flags forever. The reply
carries a marker; the tests below check that no command string can be found in
it.

AND IT MUST NOT SEND THE WRONG PERSON TO THE WRONG PLACE. Chat, review and the
agent are a workspace admin's dropdown; embeddings are the operator's .env,
because indexing ships source code to the embedder. The marker carries that
split so the page does not have to guess it — an agent that told an admin to
pick a local embedder in Settings would be describing a form that rejects
them.
"""

from __future__ import annotations

import asyncio
import json
import re
import types

import pytest

# ─── a stand-in for the small model that reads the catalogue ─────────

#: Glue that appears in every summary and every question alike. Left in, a
#: sentence would match whichever clause happened to say "the" most often.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "are", "can", "you", "your",
    "our", "what", "how", "does", "did", "not", "its", "it", "is", "of", "to",
    "in", "on", "or", "do", "from", "have", "has", "any", "about", "rather",
    "than", "such", "instead", "one", "all", "out", "was", "will", "them",
    "here", "there", "they", "when", "where", "which", "who", "why", "each",
    "set", "yet",
}


def _grams(text: str) -> tuple[set[str], set[str]]:
    """Content words and adjacent pairs, lowercased.

    Pairs are what make the spelling of a compound irrelevant: "self-hosted",
    "self hosted" and "self_hosted" all reduce to the same two tokens side by
    side. A pair of two glue words is dropped, or "what is meant" inside a
    summary about documentation would score against "what is Celmis?" — pairs
    are kept for compounds, not for grammar. Anything outside the Latin
    alphabet drops out: a lexical stand-in genuinely cannot read Ukrainian,
    and pretending otherwise here would be testing the stub instead of the
    wording.
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    unigrams = {t for t in tokens if len(t) >= 3 and t not in _STOPWORDS}
    bigrams = {f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False)
               if not (a in _STOPWORDS and b in _STOPWORDS)}
    return unigrams, bigrams


def _catalogue_from(prompt: str) -> dict[str, dict[str, str]]:
    """The verbs and argument descriptions AS THE PLANNER RECEIVES THEM.

    Read out of the prompt rather than imported from CATALOGUE: a clause that
    is in the dictionary and never reaches the model is a clause the model
    cannot choose on, and that difference is invisible if the test reads the
    dictionary too.
    """
    listing = prompt.split("Available actions:\n", 1)[1].split("\n\nRequest:")[0]
    verbs: dict[str, dict[str, str]] = {}
    current = ""
    for line in listing.splitlines():
        verb = re.match(r"- (\w+): (.*)", line)
        if verb:
            current = verb.group(1)
            verbs[current] = {"": verb.group(2)}
            continue
        arg = re.match(r"\s+(\w+): (.*)", line)
        if arg and current:
            verbs[current][arg.group(1)] = arg.group(2)
    return verbs


def _candidates(prompt: str) -> dict[tuple[str, str], str]:
    """{(action, topic): the text that argues for it}.

    `explain` is split per topic clause, because choosing the verb and
    choosing the topic is one decision for the model and the clause is all it
    has to make it on.
    """
    out: dict[tuple[str, str], str] = {}
    for name, spec in _catalogue_from(prompt).items():
        topics = spec.get("topic", "")
        if not topics:
            out[(name, "")] = " ".join(spec.values())
            continue
        for clause in topics.split("; "):
            key, _, text = clause.partition(" — ")
            assert text, f"the topic clause {clause!r} names nothing"
            out[(name, key.strip())] = text
    return out


def _read_like_a_small_model(prompt: str) -> tuple[str, str, str]:
    """Pick the best-matching (action, topic) for the request in `prompt`."""
    request = prompt.split("\n\nRequest: ", 1)[1].split("\n\n", 1)[0]
    req_uni, req_bi = _grams(request)

    scores: dict[tuple[str, str], int] = {}
    for key, text in _candidates(prompt).items():
        uni, bi = _grams(text)
        scores[key] = 2 * len(bi & req_bi) + len(uni & req_uni)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    table = ", ".join(f"{a}/{t or '-'}={s}" for (a, t), s in ranked if s)
    (action, topic), best = ranked[0]
    if best == 0 or best == ranked[1][1]:
        return "", "", f"nothing decisive matched {request!r}: {table or 'all 0'}"
    return action, topic, table


def _planner(monkeypatch):
    """`interpret` wired to the stand-in instead of a provider."""

    def _generate(**kwargs):
        action, topic, table = _read_like_a_small_model(kwargs["prompt"])
        steps = ([{"action": action, "arguments": {"topic": topic} if topic
                   else {}, "note": ""}] if action else [])
        return types.SimpleNamespace(text=json.dumps(
            {"language": "en", "note": table, "steps": steps}))

    import src.llm.client as llm_client
    monkeypatch.setattr(
        llm_client, "build_llm_client",
        lambda *a, **kw: types.SimpleNamespace(generate=_generate))


def _plan(monkeypatch, sentence: str):
    from src.automation.chat import interpret

    _planner(monkeypatch)
    return interpret(sentence, workspace_id="ws", user_id="u1")


# ─── the sentence reaches the topic ──────────────────────────────────


@pytest.mark.parametrize("sentence", [
    "how do I connect a local model?",
    "can I use Ollama?",
    "can Celmis run self-hosted on our own hardware?",
    "we are air-gapped — can this run on-prem with vLLM?",
    "does it work with LM Studio?",
    # The same question with no product name in it. Every one of these
    # reached nothing before the clause named the property rather than only
    # the vendors — and the first one reached the audit verb, which is worse
    # than nothing: a question answered with somebody else's plan card.
    "can this run offline?",
    "is there an offline mode with no internet access?",
    "is there an on-premise option?",
    "do you support on premises installs?",
    "can it run without the cloud?",
    "can we host it ourselves?",
    "run it on our own infrastructure?",
    "does our code ever leave our network?",
    "we do not want to send our code to a third party",
    # Ukrainian, which is how these arrive in practice: a Ukrainian sentence
    # around a Latin product name.
    "чи можна підключити свою модель через Ollama?",
    "чи є self-hosted режим, щоб код не виходив за межі компанії?",
    "хочу локальну модель на своєму GPU — vLLM підійде?",
])
def test_the_planner_reaches_self_hosted_from_the_words_people_type(
        monkeypatch, sentence):
    """What breaks: the clause is written in the language of the feature file
    ("bring your own inference endpoint") and the person who typed "can I use
    Ollama?" is told the agent did not understand them."""
    plan = _plan(monkeypatch, sentence)

    assert [st.action for st in plan.steps] == ["explain"], plan.note
    assert plan.steps[0].arguments == {"topic": "self_hosted"}, plan.note


@pytest.mark.parametrize("sentence,topic", [
    ("what is Celmis?", "product"),
    ("what does Celmis do?", "product"),
    ("what can this agent do?", "capabilities"),
    ("what can this agent do for me?", "capabilities"),
])
def test_the_other_two_topics_still_win_their_own_questions(
        monkeypatch, sentence, topic):
    """The new clause is the longest one in the catalogue and shares words
    with both of its neighbours. If it swallowed "what is Celmis?", the fix
    for one question would have broken the two that already worked."""
    plan = _plan(monkeypatch, sentence)

    assert [st.action for st in plan.steps] == ["explain"], plan.note
    assert plan.steps[0].arguments == {"topic": topic}, plan.note


@pytest.mark.parametrize("sentence", [
    "run a dependency audit over acme",
    "start a vulnerability audit for billing",
    "write docs for the acme repos that are missing them",
    "generate docs with claude_code for acme",
    "turn on review for billing",
    "enable automatic pull request review on the release branch",
])
def test_the_verbs_that_do_work_were_not_swallowed(monkeypatch, sentence):
    """The other direction, and the expensive one.

    Widening the clause put words in it that the rest of the catalogue uses
    too — "run", "code", "servers" — and the failure that buys is silent:
    "audit acme on main" comes back as a paragraph about running your own
    model, nothing is queued, and the person is told something true about a
    question they did not ask.

    Which verb wins is deliberately not asserted: several of these are close
    calls for a bag-of-words scorer and pinning the winner would be testing
    the stub rather than the wording. What must hold is that none of them
    turns into a question about the product.
    """
    plan = _plan(monkeypatch, sentence)

    assert "explain" not in [st.action for st in plan.steps], plan.note


@pytest.mark.parametrize("word", [
    "local model", "self-hosted", "ollama", "vllm", "lm studio",
    "own hardware", "air-gapped", "on-prem",
    # The vendorless half. "on-prem" does not contain "on-premise", and a
    # person who writes the long spelling is asking the same thing.
    "on-premise", "on premises", "offline", "no internet", "the cloud",
    "host it yourself", "infrastructure", "servers", "leave your network",
])
def test_the_planner_is_shown_the_words_a_person_would_type(word):
    """The half the lexical stand-in above cannot prove.

    "чи можна свою модель?" carries no Latin anchor at all, so whether it
    reaches this topic depends on a real model bridging it to something in
    the clause. That bridge is only as good as the vocabulary on offer: drop
    "local model" for "bring your own inference" and every phrasing that does
    not quote a vendor name stops arriving.
    """
    from src.automation.chat import CATALOGUE

    spec = CATALOGUE["explain"]
    shown = (spec["summary"] + " " + spec["arguments"]["topic"]).lower()
    assert word.replace("-", " ") in shown.replace("-", " ")


def test_the_planner_is_offered_exactly_the_topics_that_exist():
    """Both directions are silent failures. A topic the executor accepts and
    the planner is never told about is unreachable; a topic the planner is
    offered and the executor clamps away answers a question with the product
    paragraph, which reads as the agent ignoring what was asked."""
    from src.automation.chat import CATALOGUE, EXPLAIN_TOPICS

    described = {clause.partition(" — ")[0].strip()
                 for clause in CATALOGUE["explain"]["arguments"]["topic"]
                 .split("; ")}
    assert described == set(EXPLAIN_TOPICS)


# ─── the answer is a marker, and the commands are not in it ──────────


def _answer(topic: str) -> dict:
    from src.automation.actions import Actor
    from src.automation.chat import Plan, Step, execute

    plan = Plan(steps=[Step(action="explain", arguments={"topic": topic})])
    # No session: a verb that answers out of a locale file must not need one.
    outcome = asyncio.run(execute(
        plan, Actor(user_id="u", email="a@b.c", workspace_id="ws",
                    label="chat"),
        session=None))
    return outcome["steps"][0]["result"]


def test_the_answer_is_a_marker_rather_than_a_paragraph():
    """The whole reason `explain` exists. Every value here is a key or a
    surface name — nothing that has to be translated, and nothing a model had
    to be paid to write. A sentence in this dict is prose that exists in one
    language, in the one place the sixteen translations do not live."""
    marker = _answer("self_hosted")

    assert marker["topic"] == "self_hosted"
    for value in marker.values():
        for word in [value] if isinstance(value, str) else value:
            assert re.fullmatch(r"[a-z_]+", word), (
                f"{word!r} is not a key — the reply is carrying text"
            )


def test_the_commands_stay_at_the_endpoint_that_owns_them():
    """What breaks: `ollama serve` and the EMBEDDING_* lines exist twice, and
    the copy in the agent's reply is the one nobody remembers when vLLM
    renames a flag. Worse than a stale copy in source — this one is written
    onto every run row, so it is stale in history no matter what ships."""
    from src.api.routers.llm import local_setup_guide

    guide = local_setup_guide(user=None)
    # The source is a source: if this endpoint stopped carrying the commands,
    # the absence check below would pass by describing nothing.
    assert any("ollama" in opt.command.lower() for opt in guide.options)
    assert any(line.startswith("EMBEDDING_") for line in guide.env)

    reply = json.dumps(_answer("self_hosted"), ensure_ascii=False).lower()
    for opt in guide.options:
        assert opt.command.lower() not in reply, opt.name
        assert opt.base_url_hint.lower() not in reply, opt.name
    for line in guide.env:
        assert line.lower() not in reply, line
    assert guide.reindex_warning.lower() not in reply


def test_it_says_which_surfaces_are_the_reader_s_to_change():
    """The honesty that matters here. Chat, review and the agent are a
    dropdown a workspace admin can reach; embeddings are the installation's
    .env, because indexing ships source code to the embedder and that is not
    a per-workspace choice. Answered the other way round, the agent sends an
    admin to a form that refuses them and an operator to a page that has no
    field for what they need."""
    from src.llm.profiles import PROFILE_NAMES

    marker = _answer("self_hosted")

    assert "embeddings" in marker["env_surfaces"]
    assert "embeddings" not in marker["ui_surfaces"]
    assert "chat" in marker["ui_surfaces"]
    # Every surface is placed. A new one that appears in neither list is a
    # surface the answer quietly does not mention.
    assert (set(marker["ui_surfaces"]) | set(marker["env_surfaces"])
            == set(PROFILE_NAMES))


@pytest.mark.parametrize("asked", [
    "self_hosted", "self-hosted", "  Self Hosted  ", "SELF_HOSTED",
])
def test_a_hyphen_is_not_a_different_question(asked):
    """The model echoes the person's spelling, and "self-hosted" is how the
    word is written everywhere except in the topic tuple. Clamped to the
    fallback, "can I use Ollama?" gets answered with "Celmis is a…"."""
    assert _answer(asked)["topic"] == "self_hosted"


@pytest.mark.parametrize("topic", ["product", "capabilities"])
def test_the_topics_that_already_worked_are_untouched(topic):
    """They answer with the topic and nothing else, as they did before there
    was a third one — the extra fields belong to the one question that needs
    them."""
    assert _answer(topic) == {"topic": topic}


def test_explaining_this_costs_no_model_call(monkeypatch):
    """It is a switch and two constants. A model call here would make the
    cheapest question on the surface a priced one — and it would answer it in
    whichever language the model felt like."""
    import src.llm.client as llm_client

    def _boom(*a, **kw):
        raise AssertionError("the explanation went to a model")

    monkeypatch.setattr(llm_client, "build_llm_client", _boom)
    assert _answer("self_hosted")["topic"] == "self_hosted"


def test_it_answers_without_a_second_press_or_a_repository(monkeypatch):
    """It is a question, not work. A plan card asking somebody to approve
    "can I use Ollama?" is a form with one button on it, and resolving a
    repository list for it could block the answer behind a cap that has
    nothing to do with what was asked."""
    from src.automation.chat import resolve_scope

    plan = _plan(monkeypatch, "can I use Ollama?")
    plan = resolve_scope(plan, workspace_id="ws-nonexistent")

    assert plan.reads_only is True
    assert plan.blocked is None
    assert plan.steps[0].resolved_repos == []
