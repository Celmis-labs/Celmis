"""Grouping git identities: catch the same person, never merge two people.

The failure this exists for is asymmetric, and the tests are weighted the same
way. Missing a link means a developer's third identity does not join the scope
and someone ticks it by hand — annoying, visible. Making a wrong link means an
audit silently covers a repository nobody asked about, or drops one, under a
name that looks right. So the rule is narrow, it only ever SUGGESTS, and the
suggestion is confirmed by a person before anything is stored.

The identities below are invented, but shaped after the case that made the
problem visible: one developer appearing three times because git was
configured differently on their laptop, in the provider account, and not at all
on a second machine.
"""

from __future__ import annotations

import pytest

from src.repos.identity import (
    apply_aliases,
    distinctive,
    is_robot,
    suggest_groups,
    tokens,
)

#: Fictional, but written the way a real scan writes them.
IDENTITIES = [
    "bladek33@gmail.com",
    "Taras Melnyk",
    "Olha Kovalenko",
    "olha.kovalenko.ua@example.com",
    "olhakovalenko@Laptop-Olha.local",
    "Andrii Zaverukha",
    "a_zaverukha@acme.com.ua",
    "Dmytro Bondarenko",
    "bond.dmitr@gmail.com",
    "o_marchenko@acme.ua",
]


def group_of(identity: str, identities: list[str] = IDENTITIES) -> str:
    return suggest_groups(identities)[identity]


def test_one_developer_written_three_ways_is_one_suggestion():
    """The case that started this: provider name, laptop email, machine host."""
    olha = {
        "Olha Kovalenko",
        "olha.kovalenko.ua@example.com",
        "olhakovalenko@Laptop-Olha.local",
    }
    labels = {group_of(i) for i in olha}
    assert len(labels) == 1, f"Olha split across {labels}"


def test_an_initial_and_a_surname_reach_the_written_name():
    """`a_zaverukha@acme.com.ua` beside "Andrii Zaverukha"."""
    assert group_of("a_zaverukha@acme.com.ua") == group_of("Andrii Zaverukha")


def test_a_shortened_first_name_links_only_as_a_prefix():
    """`bond.dmitr` beside "Dmytro Bondarenko" — "bond" opens "bondarenko".

    Four characters is too weak to link on its own, so it counts only at the
    start of a longer token. A four-letter run appearing somewhere in the
    middle of an unrelated name means nothing and must not link.
    """
    assert group_of("bond.dmitr@gmail.com") == group_of("Dmytro Bondarenko")
    assert suggest_groups(["mike@x.com", "yamikoto@y.com"])["mike@x.com"] == "mike@x.com"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("bladek33@gmail.com", "Taras Melnyk"),
        ("o_marchenko@acme.ua", "a_zaverukha@acme.com.ua"),
        ("Dmytro Bondarenko", "Andrii Zaverukha"),
        ("bond.dmitr@gmail.com", "bladek33@gmail.com"),
        ("Olha Kovalenko", "Taras Melnyk"),
    ],
)
def test_different_people_stay_apart(a: str, b: str):
    assert group_of(a) != group_of(b), f"{a} was merged into {b}"


def test_a_shared_employer_is_not_a_shared_person():
    """Only the local part of an email is read.

    Reading the domain would put every colleague at one company in a single
    bucket — the exact opposite of what a developer filter is for.
    """
    # Three colleagues, three distinct local parts. Invented, like the rest of
    # this file: the de-identification pass renamed the addresses that appear
    # beside a written name and left these three, whose surnames came from the
    # same production workspace.
    same_domain = ["a_bilenko@acme.ua", "o_hrytsenko@acme.ua", "i_verbytsky@acme.ua"]
    assert len(set(suggest_groups(same_domain).values())) == 3


def test_machine_and_service_words_never_link_anyone():
    """`admin@MacBook-Pro.local` and `admin@ubuntu` are two machines, not a
    person called Admin."""
    hosts = ["admin@MacBook-Pro.local", "admin@ubuntu", "runner@github"]
    groups = suggest_groups(hosts)
    assert len(set(groups.values())) == 3, groups


def test_the_label_is_the_name_a_human_recognises():
    labels = set(suggest_groups(IDENTITIES).values())
    assert "Olha Kovalenko" in labels
    assert "olhakovalenko@Laptop-Olha.local" not in labels


def test_the_label_does_not_move_between_calls():
    """A group key that changes between requests would break a saved scope."""
    first = suggest_groups(IDENTITIES)
    second = suggest_groups(list(reversed(IDENTITIES)))
    assert first == second


def test_a_confirmed_grouping_beats_any_guess():
    """What a person saved is the answer, and it says nothing about the rest."""
    mapping = apply_aliases(
        ["Olha Kovalenko", "olha.kovalenko.ua@example.com", "Taras Melnyk"],
        {"Olha K.": ["Olha Kovalenko", "olha.kovalenko.ua@example.com"]},
    )
    assert mapping["Olha Kovalenko"] == "Olha K."
    assert mapping["olha.kovalenko.ua@example.com"] == "Olha K."
    assert mapping["Taras Melnyk"] == "Taras Melnyk"


def test_an_alias_naming_an_unknown_identity_changes_nothing():
    mapping = apply_aliases(["a@x.com"], {"Someone": ["gone@old.com"]})
    assert mapping == {"a@x.com": "a@x.com"}


def test_digits_and_short_fragments_are_not_distinctive():
    assert distinctive("2024@build.local") == set()
    assert "dev" not in distinctive("dev@x.com")
    assert tokens("olha.kovalenko.ua@example.com") == {"olha", "kovalenko", "ua"}


@pytest.mark.parametrize("identity", [
    "root@agents.acme.tech",
    "root@billing.acme.tech",
    "ci@build.example",
    "dependabot[bot]",
    "gitlab-runner@runners.internal",
])
def test_a_machine_is_not_a_colleague(identity: str):
    """The shapes a provider scan turns up.

    Commits like these exist — someone deployed as root on a server — so they
    are reported rather than dropped. But a people-picker that lists
    `root@agents.acme.tech` between two colleagues is asking the user to
    decide whether a server wrote the code.
    """
    assert is_robot(identity), identity


@pytest.mark.parametrize("identity", [
    "bladek33@gmail.com",
    "Yaroslav Tkachuk",
    "olhakovalenko@Laptop-Olha.local",
    "verbytskyi@gmail.com",
    "Kyrylo",
])
def test_a_person_is_never_mistaken_for_a_machine(identity: str):
    """The machine-local address is the trap.

    `name@Someones-MacBook.local` looks synthetic and is a person whose git was
    never configured. Hiding them would lose their work from every scope —
    which is the failure this whole feature exists to prevent.
    """
    assert not is_robot(identity), identity
