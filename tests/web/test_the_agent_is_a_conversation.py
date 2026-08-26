"""The agent page is a thread, and the things a thread must not lose.

The page used to be a form with a receipt printer: one question at the top, one
plan card under it, and a list of everything ever asked at the bottom. Rewriting
it into a transcript moved every one of those parts, and each move had a way of
quietly breaking something that no type checker and no build can see.

What is pinned here, and what breaks without it:

THE CAPABILITIES MESSAGE IS A STRING, NOT AN ANSWER. "What can you do" has one
answer, it changes only when the catalogue changes, and asking a model to recite
six verbs — in whichever of sixteen languages was asked — is money spent on a
string that could be a string. So it is locale text rendered by the client. The
danger of writing it down is that it stops matching `src/automation/chat.py`
CATALOGUE, and a capability list that is confidently wrong is worse than none:
somebody reads "I can audit dependencies", types it, and is told it is not an
action. So the list on the page is checked against the catalogue itself, verb
for verb, and every verb is checked for a line in every locale.

IT IS SHOWN TWICE. On an empty thread, where it is the opening turn, and again
under any reply that recognised no action at all — which is exactly the moment
somebody needs to know what the vocabulary is. Losing the second one leaves a
dead end that says only "I could not read that as an action".

THE SESSION ID BELONGS TO THE BROWSER. The server groups rows by whatever id it
is handed; only the browser knows where one sitting ends. If the id is
regenerated on load, a reload splits one conversation into two threads, and the
half that was mid-plan becomes unreachable from the sessions list.

THE THREAD IS THE SERVER'S. History is fetched per session and the sessions
list is rendered, because a reading that survives the page is only useful if
the page can find it again.

READING IS POLLED, AND STOP IS OFFERED ONLY WHILE IT IS. A run left "reading"
with no poll is a spinner that never resolves; a Stop button on a finished run
is a button whose endpoint answers 409.

AND NOTHING THAT COSTS MONEY RUNS ON ONE PRESS. That gate is the whole reason
this surface is allowed to exist, and a redesign is precisely when it gets
optimised away — an `onSuccess` that confirms, an effect that runs the plan it
just received.

Source is parsed rather than grepped wherever it can be. Comments here NAME the
thing they explain the absence of — "the reading no longer happens in the
request", "this file no longer enqueues" — so a test that greps the raw text
finds its token in the sentence saying it is gone. That has happened on this
repository five times. `_code()` strips TSX comments; `_python_code()` strips
Python comments and docstrings — by token and by node, never by `ast.unparse`,
which keeps every docstring it is handed.
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

from tests.web.test_rules_of_hooks import FATAL_RULES, WEB, _eslint_available

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
PAGE = WEB / "app" / "(app)" / "automation" / "page.tsx"
MESSAGES = WEB / "lib" / "i18n" / "messages"

EN = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))


# ─── reading the source without reading the comments ─────────────────


def _code(source: str) -> str:
    """TSX with its comments removed and its string literals intact.

    A scanner is used rather than a pair of regexes because the interesting
    strings on this page — the endpoint paths, the locale keys — sit next to
    the comments that explain them, and a `//` inside a template literal must
    not eat the rest of the line.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "/" and source[i + 1:i + 2] == "/":
            while i < n and source[i] != "\n":
                i += 1
        elif c == "/" and source[i + 1:i + 2] == "*":
            end = source.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c in "\"'`":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if source[i] == "\\":
                    out.append(source[i:i + 2])
                    i += 2
                    continue
                out.append(source[i])
                if source[i] == quote:
                    i += 1
                    break
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _python_code(text: str) -> str:
    """Python with comments and docstrings removed, other literals intact.

    `ast.unparse` would be the obvious tool and is the wrong one: it keeps
    docstrings, which is where a sentence like "this no longer enqueues"
    lives, and it rewrites the literals a check like this is looking for. So
    the comments come out by token and the docstrings by node, and everything
    else is left exactly as written.
    """
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def at(row: int, col: int) -> int:
        return starts[row - 1] + col

    cuts: list[tuple[int, int]] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            cuts.append((at(*tok.start), at(*tok.end)))
    for node in ast.walk(ast.parse(text)):
        # A bare string statement: a docstring, or prose left in the body.
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            cuts.append((at(node.lineno, node.col_offset),
                         at(node.end_lineno, node.end_col_offset)))
    for start, end in sorted(cuts, reverse=True):
        text = text[:start] + text[end:]
    return text


def _python_source(path: Path) -> str:
    return _python_code(path.read_text(encoding="utf-8"))


def _squash(source: str) -> str:
    """One space for any run of whitespace, so an assertion survives a
    reformat that only moved a line break."""
    return re.sub(r"\s+", " ", source)


def _pair(code: str, at: int, opener: str, closer: str) -> tuple[int, int]:
    begin = code.index(opener, at)
    depth = 0
    for i in range(begin, len(code)):
        if code[i] == opener:
            depth += 1
        elif code[i] == closer:
            depth -= 1
            if depth == 0:
                return begin, i
    raise AssertionError(f"unbalanced {opener} from {at}")


def _body(code: str, name: str) -> str:
    """The body of `function name(...)`, by matching brackets rather than
    guessing where it ends — the parameter lists here are destructured, so the
    first `{` after the name is not the body."""
    at = code.index(f"function {name}(")
    _, params_end = _pair(code, at, "(", ")")
    begin, end = _pair(code, params_end, "{", "}")
    return code[begin + 1:end]


def _array(code: str, name: str) -> list[str]:
    """The string elements of `const NAME = [...]`."""
    at = code.index(f"const {name} = [")
    begin, end = _pair(code, at, "[", "]")
    return re.findall(r'"([^"]+)"', code[begin:end + 1])


_TOKEN = re.compile(r"[{}()\[\]]|\breturn\b|\buse[A-Z]\w*(?=\s*[(<])")


def _hooks_after_first_return(body: str) -> list[str]:
    """Hook calls that sit below an early return in a component body.

    Order, not presence. Returns inside a nested callback do not count — an
    early return in a `useEffect` body is ordinary code — so the scan tracks
    whether it is inside an arrow function, which is what `=>` before the
    brace says. Generic hooks (`useState<string[]>(`) are matched too; the
    lookahead is what keeps their opening paren countable as a bracket.
    """
    stack: list[str] = []
    guard: int | None = None
    late: list[str] = []
    for m in _TOKEN.finditer(body):
        tok = m.group(0)
        if tok == "{":
            stack.append("fn" if body[:m.start()].rstrip().endswith("=>")
                         else "block")
        elif tok in "([":
            stack.append("group")
        elif tok in ")]}":
            if stack:
                stack.pop()
        elif tok == "return":
            if "fn" not in stack and guard is None:
                guard = m.start()
        elif "fn" not in stack and guard is not None:
            late.append(f"{tok} at offset {m.start()}")
    return late


CODE = _code(PAGE.read_text(encoding="utf-8"))
PAGE_BODY = _squash(_body(CODE, "AutomationPage"))
REPLY_BODY = _squash(_body(CODE, "Reply"))
CAPABILITIES_BODY = _squash(_body(CODE, "Capabilities"))


def test_the_comment_stripper_actually_strips():
    """Guards the guard. Every assertion below is a claim about code, and if
    the stripper let comments through they would all also be claims about the
    prose explaining why something is not there."""
    sample = _code(
        '// t("automation.ghost")\n'
        '/* api("/api/ghost") */\n'
        'const real = t("automation.real"); // and t("automation.trailing")\n'
        'const url = "https://example.test/not-a-comment";\n'
    )
    assert "automation.ghost" not in sample
    assert "/api/ghost" not in sample
    assert "automation.trailing" not in sample
    assert 't("automation.real")' in sample
    assert "https://example.test/not-a-comment" in sample, (
        "a URL inside a string was eaten as a comment"
    )


def test_the_python_stripper_drops_docstrings_and_comments():
    """The same guard on the other side. The docstrings in `automation.py` and
    `handlers.py` describe at length what those files no longer do — "the
    reading used to happen inside this request" is a sentence containing every
    token a careless check would look for."""
    stripped = _python_code(
        'def read():\n'
        '    """It no longer calls enqueue() or execute(plan) here."""\n'
        '    # and never status="answered"\n'
        '    return finish(status="planned")\n'
    )
    assert "enqueue()" not in stripped
    assert "execute(plan)" not in stripped
    assert 'status="answered"' not in stripped
    assert 'finish(status="planned")' in stripped, "the code was stripped too"


def test_the_page_still_parses_as_the_shape_these_tests_assume():
    """A rename of the components makes every extraction below silently read
    an empty string, and empty strings satisfy nothing — the tests would fail
    loudly, but on the wrong thing. This says which thing."""
    for name in ("AutomationPage", "Reply", "Capabilities", "Answer"):
        assert f"function {name}(" in CODE, f"{name} is gone or renamed"
    assert len(PAGE_BODY) > 2000 and len(REPLY_BODY) > 1000


# ─── the capabilities message is written down, not generated ─────────


def _reads_and_writes() -> tuple[list[str], list[str]]:
    from src.automation.chat import CATALOGUE

    reads = [v for v, spec in CATALOGUE.items() if spec.get("reads")]
    writes = [v for v, spec in CATALOGUE.items() if not spec.get("reads")]
    return reads, writes


def test_the_capabilities_message_names_every_verb_in_the_catalogue():
    """The failure this exists to prevent is silent drift.

    A verb added to CATALOGUE and not to this list is a capability nobody is
    told about, which is merely a waste. A verb removed from CATALOGUE and left
    in this list is worse: the page advertises something, the person types it,
    and the model — which is only ever given the catalogue — refuses. The list
    is confidently wrong, and confidently wrong is worse than absent.
    """
    reads, writes = _reads_and_writes()
    assert _array(CODE, "READS") == reads, (
        "the questions the page advertises are not the catalogue's reads"
    )
    assert _array(CODE, "WRITES") == writes, (
        "the work the page advertises is not the catalogue's writes"
    )


def test_the_split_the_message_draws_is_the_split_the_backend_makes():
    """Reads answer immediately and writes wait for a press. Advertising a
    write under "answered straight away" promises something that will instead
    put a card in front of somebody, and advertising a read under "run on a
    second press" makes a free question look expensive."""
    reads, writes = _reads_and_writes()
    assert set(reads) & set(writes) == set()
    listed = _array(CODE, "READS") + _array(CODE, "WRITES")
    assert sorted(listed) == sorted(reads + writes)
    assert len(listed) == len(set(listed)), "a verb is advertised twice"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_verb_has_a_name_and_a_line_in_every_locale(locale: str):
    """Sixteen locales, and the message is rendered from keys. A missing key
    renders as the raw key id — `automation.capabilities.list_findings` in the
    middle of a sentence — which is how it ships without anybody noticing."""
    from src.automation.chat import CATALOGUE

    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    for verb in CATALOGUE:
        for key in (f"automation.action.{verb}",
                    f"automation.capabilities.{verb}"):
            assert data.get(key, "").strip(), f"{locale} has no {key}"


@pytest.mark.parametrize("locale", [loc for loc in LOCALES if loc != "en"])
def test_no_locale_reads_the_capabilities_message_back_in_english(locale: str):
    """A key copied from `en.json` to make the completeness test pass is a key
    that is present and useless: the person asked in Polish and is told, in
    English, what the thing can do.

    Scoped to the capabilities message rather than every automation string,
    because a one-word label can legitimately be the same word — Czech, Polish
    and Slovak all really do say "Stop".
    """
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    echoed = [k for k, v in EN.items()
              if k.startswith("automation.capabilities.") and data.get(k) == v]
    assert not echoed, f"{locale} carries the English text for: {echoed}"


def test_the_message_costs_nothing_to_render():
    """It exists because "what can you do" should not be a model call. A fetch
    inside this component would put it back on the network — cheaper than a
    model call, and still a spinner, an error state and a reason for the list
    to be missing exactly when somebody is lost."""
    for forbidden in ("api(", "useQuery", "useMutation", "fetch(", ".mutate("):
        assert forbidden not in CAPABILITIES_BODY, (
            f"the capabilities message calls {forbidden} — it is meant to be "
            "a string"
        )


def test_no_sentence_is_sent_to_the_agent_except_the_one_that_was_typed():
    """The other way this becomes a model call: a canned "what can you do"
    posted to /plan on mount. The plan mutation is fired from exactly one
    place, and that place is the composer."""
    assert PAGE_BODY.count("propose.mutate(") == 1
    assert "propose.mutate(text)" in PAGE_BODY
    send = PAGE_BODY[PAGE_BODY.index("const send = () =>"):]
    assert "const text = draft.trim();" in send[:200]


def test_the_server_never_produces_the_capabilities_text():
    """If it did, this would be a model call again — or worse, sixteen
    translations living somewhere the translators do not look."""
    offenders = []
    for path in SRC.rglob("*.py"):
        raw = path.read_text(encoding="utf-8")
        if "automation.capabilities" not in raw:
            continue  # cheap filter; the strip below is the real check
        if "automation.capabilities" in _python_source(path):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        f"the capabilities message is built on the server in {offenders}"
    )


def test_a_count_in_the_message_is_the_catalogue_size():
    """"Six things" is a number written in a sentence in sixteen languages,
    and the seventh verb will not update any of them. Only English is pinned —
    it is the source the other fifteen are translated from, so a mismatch here
    is caught before the translations are ordered."""
    from src.automation.chat import CATALOGUE

    words = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10}
    intro = EN["automation.capabilities.intro"].lower()
    claimed = [n for word, n in words.items()
               if re.search(rf"\b{word}\b", intro)]
    for n in claimed:
        assert n == len(CATALOGUE), (
            f"the message says {n} things and the catalogue has "
            f"{len(CATALOGUE)}"
        )


def test_the_examples_are_locale_strings_too():
    """They are the documentation — one press puts a working sentence in the
    composer — so an untranslated one is a sentence the model is then asked to
    read in the wrong language."""
    examples = _array(CODE, "EXAMPLES")
    assert len(examples) >= 3
    for locale in LOCALES:
        data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
        for key in examples:
            assert data.get(key, "").strip(), f"{locale} has no {key}"
    # At least one example is a question, so the reads half of the catalogue
    # is reachable by pressing rather than by knowing.
    assert "automation.exRead" in examples


# ─── shown when there is nothing, and when nothing was understood ────


def test_the_message_opens_an_empty_thread():
    """An empty transcript with a composer under it asks a question — "what am
    I allowed to type" — and answers it nowhere."""
    assert "{thread.length === 0 && <Capabilities" in PAGE_BODY


def test_the_message_comes_back_when_a_reply_recognised_nothing():
    """The dead end this closes: "I could not read that as an action", alone,
    from a surface whose vocabulary is six verbs the person cannot see."""
    assert ('const unread = run.status !== "reading" && run.steps.length === 0;'
            in REPLY_BODY), "the no-steps case is no longer detected"
    assert "{unread && <Capabilities" in REPLY_BODY


def test_a_run_still_being_read_is_not_called_unrecognised():
    """A reading run has no steps yet. Without the status guard the page would
    accuse the model of not understanding a sentence it has not finished
    reading."""
    unread = REPLY_BODY[REPLY_BODY.index("const unread ="):]
    unread = unread[:unread.index(";")]
    assert 'run.status !== "reading"' in unread


# ─── the session id is the browser's, and survives a reload ──────────


def test_the_session_id_is_stored_and_the_stored_one_wins():
    """A fresh uuid per load splits one conversation into two threads: the
    sentence and its plan land under different ids, the composer posts into a
    thread the sessions list shows as a separate sitting, and the plan the
    person was about to approve is one click away in a list they do not know
    to open."""
    read = _squash(_body(CODE, "readSessionId"))
    assert "localStorage.getItem(SESSION_KEY)" in read
    assert "cachedSessionId = stored || newSessionId();" in read, (
        "a stored session id no longer wins over a freshly generated one"
    )
    assert "localStorage.setItem(SESSION_KEY, sid);" in _squash(
        _body(CODE, "persistSessionId"))


def test_reading_the_session_id_survives_storage_being_unavailable():
    """Private mode throws on `localStorage`. An uncaught throw here happens
    during render of a client component and takes the page down."""
    read = _squash(_body(CODE, "readSessionId"))
    assert "try {" in read and "} catch" in read


def test_the_session_id_is_read_as_an_external_store():
    """It is state that lives outside React, and copying it into `useState`
    from an effect is the shape that breaks twice: the prerender has no
    `localStorage`, and a mount-time `setState` cascades a second render on
    every visit. The server snapshot is null so the first paint matches the
    HTML."""
    assert "useSyncExternalStore(" in PAGE_BODY
    assert "const sessionId = useSyncExternalStore(" in PAGE_BODY
    assert "return null;" in _squash(_body(CODE, "serverSessionId"))


def test_every_sentence_carries_the_session_id():
    """The id groups rows for reading back. A sentence posted without one
    lands in the day-bucket the sessions list invents, not in the thread it
    was typed into."""
    propose = PAGE_BODY[PAGE_BODY.index("const propose = useMutation("):]
    propose = propose[:propose.index("const stop = useMutation(")]
    assert '"/api/automation/plan"' in propose
    assert "session_id: sessionId" in propose


def test_a_new_chat_is_a_new_id_that_is_also_written_down():
    """"New chat" that only clears the screen leaves the next sentence in the
    old thread; one that changes the id without persisting it is undone by the
    next reload."""
    start = _squash(_body(CODE, "AutomationPage"))
    at = start.index("const startNewChat = () =>")
    assert "newSessionId()" in start[at:at + 200]
    assert "selectSession(sid)" in start[at:at + 200]
    switch = _squash(_body(CODE, "switchSession"))
    assert "persistSessionId(sid);" in switch
    assert "sessionListeners.forEach" in switch, (
        "switching sessions does not notify the subscribed page"
    )


# ─── the thread, and the list of threads ─────────────────────────────


def test_history_is_asked_for_one_session_at_a_time():
    """A thread is the rows of one sitting. Fetching the workspace's whole
    history into the transcript would put somebody else's sentences under
    yours, in the order they happened to be written."""
    history = PAGE_BODY[PAGE_BODY.index("const history = useQuery("):]
    history = history[:history.index("const thread = useMemo(")]
    assert "/api/automation/history?limit=50&session_id=${encodeURIComponent(" \
        in history
    assert '["automation-history", sessionId]' in history, (
        "the cache key does not name the session, so switching sessions "
        "shows the previous thread until the refetch lands"
    )
    assert "enabled: !!token && !!sessionId" in history


def test_the_thread_reads_downwards():
    """The wire order is newest first, which is right for a list and backwards
    for a transcript."""
    assert "[...(history.data ?? [])].reverse()" in PAGE_BODY


def test_the_sessions_list_is_fetched_and_rendered():
    """Rows that survive the page are only useful if the page can find them.
    Without the list, a reading that finished after somebody closed the tab is
    reachable only by not having closed the tab."""
    assert '"/api/automation/sessions?limit=30"' in PAGE_BODY
    assert '["automation-sessions"]' in PAGE_BODY
    assert "(sessions.data ?? []).map((s) => (" in PAGE_BODY
    assert "selectSession(s.session_id)" in PAGE_BODY, (
        "the sessions are listed but cannot be opened"
    )
    assert "{s.title || t(\"automation.sessionUntitled\")}" in PAGE_BODY


def test_asking_something_new_updates_the_list_of_sessions():
    """The first sentence of a sitting is its title. Without the
    invalidation the new thread is missing from the sidebar until a reload,
    which reads as the sentence having gone nowhere."""
    propose = PAGE_BODY[PAGE_BODY.index("const propose = useMutation("):]
    propose = propose[:propose.index("const stop = useMutation(")]
    assert 'invalidateQueries({ queryKey: ["automation-sessions"] })' in propose
    assert 'setQueryData<Run[]>(["automation-history", sessionId]' in propose, (
        "the sentence is not shown until the refetch lands, which reads as a "
        "lost message"
    )


# ─── polling while reading, and Stop only then ───────────────────────


def test_a_run_being_read_keeps_polling():
    """The reading happens on the queue, so the page learns it finished only
    by asking. No poll and the spinner never resolves — while the answer sits
    in the database."""
    history = PAGE_BODY[PAGE_BODY.index("const history = useQuery("):]
    history = history[:history.index("const thread = useMemo(")]
    at = history.index("refetchInterval:")
    interval = history[at:history.index("});", at)]
    assert '.some((r) => r.status === "reading")' in interval, (
        "the poll no longer depends on something actually being read"
    )
    assert re.search(r"\?\s*\d+", interval), "no poll interval"


def test_a_finished_thread_stops_asking():
    """A page left open on yesterday's conversation polling every 1.2 s is a
    request per second per open tab, forever, for a row that cannot change."""
    history = PAGE_BODY[PAGE_BODY.index("const history = useQuery("):]
    history = history[:history.index("const thread = useMemo(")]
    at = history.index("refetchInterval:")
    interval = history[at:history.index("});", at)]
    assert ": false" in interval, "the poll never turns itself off"


def test_stop_is_offered_only_while_something_is_being_read():
    """Stop stops a job. Offering it on a finished row offers a button whose
    endpoint answers 409 — the contract is explicit about that — and the person
    gets an error toast for pressing what they were shown."""
    reading_branch = REPLY_BODY[REPLY_BODY.index(
        'if (run.status === "reading") {'):]
    reading_branch = reading_branch[:reading_branch.index("} return (")]
    assert 't("automation.stop")' in reading_branch
    assert REPLY_BODY.count('t("automation.stop")') == 1, (
        "Stop is rendered outside the reading branch"
    )
    assert REPLY_BODY.count("onStop") == 1


def test_stop_asks_the_endpoint_that_stops_the_reading():
    stop = PAGE_BODY[PAGE_BODY.index("const stop = useMutation("):]
    stop = stop[:stop.index("const confirm = useMutation(")]
    assert "/api/automation/runs/${runId}/stop" in stop
    assert '"POST"' in stop


def test_the_server_agrees_that_stop_is_for_a_reading():
    """The rule the button is obeying. If the server ever allowed stopping a
    started run, the page's rule would be the only thing preventing a
    half-cancelled sweep."""
    router = _python_source(SRC / "api" / "routers" / "automation.py")
    stop_fn = router[router.index("async def stop_run("):
                     router.index("class ExecuteIn")]
    assert 'row.status != "reading"' in _squash(stop_fn)
    assert "status_code=409" in stop_fn


# ─── nothing that starts work runs on one press ──────────────────────


def test_reading_a_sentence_never_runs_it():
    """The gate, on the page. Interpretation is a guess and these verbs cost
    hours of model time: a plan is shown, and a person presses again."""
    propose = PAGE_BODY[PAGE_BODY.index("const propose = useMutation("):]
    propose = propose[:propose.index("const stop = useMutation(")]
    assert "execute" not in propose, (
        "the plan request also executes — there is no second press"
    )
    assert "confirm.mutate" not in propose


def test_the_execute_call_exists_once_and_hangs_off_a_press():
    """The shapes that quietly remove the gate are an `onSuccess` that
    confirms and an effect that runs whatever it just received. Both would put
    a second `confirm.mutate` somewhere that is not an `onClick`."""
    assert PAGE_BODY.count('"/api/automation/execute"') == 1
    assert PAGE_BODY.count("confirm.mutate(") == 1
    assert "onConfirm={() => confirm.mutate(h.id)}" in PAGE_BODY
    for effect in re.findall(r"useEffect\(\(\) => \{(.*?)\}, \[", PAGE_BODY):
        assert "mutate" not in effect, f"an effect starts work: {effect[:120]}"


def test_the_run_button_is_only_on_a_plan_that_is_waiting():
    """An answered read has nothing to approve; a blocked plan must not be
    approvable at all, or the refusal arrives after the press."""
    assert ('{run.status === "planned" && !run.blocked && run.steps.length > 0 '
            "&& (" in REPLY_BODY)
    at = REPLY_BODY.index('{run.status === "planned" && !run.blocked')
    gate = REPLY_BODY[at:at + 700]
    assert 't("automation.confirm")' in gate
    assert REPLY_BODY.count("onClick={onConfirm}") == 1


def test_the_plan_shows_the_repositories_before_the_press():
    """The count is not the point, the list is: "all of them" meaning forty
    instead of four is obvious in a list and invisible in a sentence. This is
    the only screen where a misreading is visible before it costs anything."""
    assert '{run.status === "planned" && run.steps.map((s, i) => (' in REPLY_BODY
    assert "{s.resolved_repos.map((r) => (" in REPLY_BODY
    assert "{s.blocked && (" in REPLY_BODY, (
        "a step's refusal is not shown next to the step"
    )


def test_cancelling_a_plan_is_a_local_act():
    """The row stays "planned" on the server so the thread still reads as
    "this was asked and not run" — a cancel that deleted it would lose the
    record of having decided against something."""
    assert "const [dismissed, setDismissed] = useState<string[]>([]);" in PAGE_BODY
    assert "setDismissed((d) => [...d, h.id])" in PAGE_BODY
    assert 't("automation.notRun")' in REPLY_BODY


def test_the_server_only_runs_a_plan_it_wrote_down_itself():
    """The page's gate is a courtesy; this is the one that holds. /execute
    takes an id, not a plan, and refuses anything that is not a stored reading
    still waiting for a press — so a client cannot post a scope of its own
    invention, and a second press cannot run the same sweep twice."""
    router = _python_source(SRC / "api" / "routers" / "automation.py")
    execute_fn = router[router.index("async def execute_plan("):]
    assert 'row.status not in ("planned",)' in _squash(execute_fn)
    assert "status_code=409" in execute_fn
    assert "resolve_scope(" in execute_fn, (
        "the stored scope is replayed rather than re-resolved"
    )


def test_a_read_is_answered_and_a_write_is_left_waiting():
    """Where the two statuses on the wire come from. If the worker ever marked
    a write "answered", the page would render its result and never offer the
    press — and the work would never have run."""
    handlers = _squash(_python_source(SRC / "sync" / "handlers.py"))
    at = handlers.index("async def handle_automation_plan(")
    body = handlers[at:handlers.index("async def _run_automation_reads(")]
    assert "if plan.reads_only and not plan.blocked:" in body
    assert 'status="answered"' in body
    assert 'status="planned"' in body
    assert body.index('status="answered"') < body.index(
        'status="planned"'), "reads no longer take the answering branch"


# ─── rules of hooks, in the rewritten page ───────────────────────────


@pytest.mark.skipif(not _eslint_available(), reason="web deps not installed")
def test_no_hook_on_this_page_is_called_conditionally():
    """The same lint rule `test_rules_of_hooks` runs over the whole app, aimed
    at the file this rewrite touched. Four components were reshuffled here, one
    of which returns early while a run is being read — the exact shape that
    took the site down the last time."""
    proc = subprocess.run(
        [str(WEB / "node_modules" / ".bin" / "eslint"),
         str(PAGE.relative_to(WEB)), "--format", "json"],
        cwd=WEB, capture_output=True, text=True, timeout=300,
    )
    try:
        report = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:  # pragma: no cover - eslint itself broke
        pytest.fail(f"eslint produced no parseable report:\n{proc.stderr[-2000:]}")
    assert report, "eslint linted no files — the path is wrong"
    violations = [
        f"{Path(f['filePath']).name}:{m['line']} {m['message']}"
        for f in report
        for m in f.get("messages", [])
        if m.get("ruleId") in FATAL_RULES
    ]
    assert not violations, (
        "a hook is called conditionally — React error #310 unmounts the page "
        "tree at runtime:\n  " + "\n  ".join(violations)
    )


@pytest.mark.parametrize(
    "component", ["AutomationPage", "Reply", "Capabilities", "Answer"])
def test_every_component_calls_its_hooks_before_any_early_return(component: str):
    """The story the lint rule tells, readable without running node.

    It checks ORDER, not presence: presence is what the guard written for the
    outage checked, and presence was true throughout it.
    """
    late = _hooks_after_first_return(_body(CODE, component))
    assert not late, (
        f"{component} calls {late} after a return; the first render returns "
        "early and the next one does not, so React counts a different number "
        "of hooks and throws #310"
    )


def test_the_hook_scan_would_notice_the_shape_it_is_looking_for():
    """An order check that cannot fail is the guard this file is trying not to
    be. Fed the outage's exact shape, the scan must object — and it must stay
    quiet about the two shapes that look like it and are fine."""
    caught = _hooks_after_first_return(
        "const [a, setA] = useState(0); if (!jwt) return null; "
        "useEffect(() => {}, []);"
    )
    assert [c.split()[0] for c in caught] == ["useEffect"], caught

    # A return inside a callback, with a hook after it: ordinary code.
    assert _hooks_after_first_return(
        "useEffect(() => { if (!el) return; el.focus(); }, []); "
        "const [a] = useState<string[]>([]); return null;"
    ) == []
    # Hooks first, one return at the end: every component on this page.
    assert _hooks_after_first_return(
        "const t = useT(); const [a] = useState(0); return <div />;"
    ) == []
