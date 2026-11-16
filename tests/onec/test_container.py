"""The container reader: entries, dates, raw deflate, nesting and the limits
that stop a hostile file before it costs memory."""

from __future__ import annotations

import struct
from datetime import datetime

import pytest

from src.onec.container import (
    ContainerError,
    Limits,
    form_module,
    is_container,
    read_container,
    text_of,
)
from tests.onec.container_writer import _block, container, form_bin

WHEN = datetime(2025, 3, 4, 10, 30)


def test_entries_and_dates_read_back():
    box = read_container(container([("one", b"first"), ("two", b"second")], when=WHEN))
    assert [e.name for e in box.entries] == ["one", "two"]
    assert box.get("two").data == b"second"
    assert box.dates() == (WHEN, WHEN)


def test_deflated_top_level_is_inflated():
    box = read_container(container([("text", "Привет".encode("utf-8-sig"))], deflate=True))
    entry = box.get("text")
    assert entry.deflated
    assert text_of(entry.data) == "Привет"


def test_nested_containers_are_walked():
    inner = container([("module", b"code")])
    box = read_container(container([("outer", inner)], deflate=True))
    paths = [p for p, _e in box.walk()]
    assert paths == ["outer", "outer/module"]
    assert box.get("outer").child("module").data == b"code"


def test_form_module_of_a_form_bin():
    data = form_bin("{27}", "Процедура А()\nКонецПроцедуры\n")
    assert form_module(data).startswith("Процедура А()")
    assert form_module(form_bin("{27}", None)) is None


def test_not_a_container():
    assert not is_container(b"PK\x03\x04 a zip")
    with pytest.raises(ContainerError):
        read_container(b"plain text, not 1C at all")


def test_an_entry_past_max_entry_is_refused():
    big = b"\0" * 200_000  # deflates to a few hundred bytes: a small bomb
    data = container([("bomb", big)], deflate=True)
    with pytest.raises(ContainerError):
        read_container(data, Limits(max_entry=100_000))


def test_too_many_entries_are_refused():
    data = container([(f"e{i}", b"x") for i in range(30)])
    with pytest.raises(ContainerError):
        read_container(data, Limits(max_entries=10))


def test_nesting_stops_at_max_depth():
    data = b"leaf"
    for i in range(6):
        data = container([(f"level{i}", data)])
    box = read_container(data, Limits(max_depth=2))
    paths = [p for p, _e in box.walk()]
    # Three levels are read; the next container is kept as bytes, not opened.
    assert len(paths) == 3
    deepest = list(box.walk())[-1][1]
    assert deepest.children is None and is_container(deepest.data)


def test_a_block_chain_that_loops_is_refused():
    # One entry whose data block names itself as the next block.
    head = struct.pack("<QQI", 0, 0, 0) + "loop".encode("utf-16-le") + b"\0\0"
    toc_at, h_at = 16, 16 + 31 + 12
    hb = _block(head)
    d_at = h_at + len(hb)
    db = _block(b"data", nxt=d_at, size=1000)
    toc = _block(struct.pack("<III", h_at, d_at, 0x7FFFFFFF))
    data = struct.pack("<IIII", 0x7FFFFFFF, 512, 0, 0) + toc + hb + db
    assert len(struct.pack("<IIII", 0, 0, 0, 0)) == toc_at
    with pytest.raises(ContainerError):
        read_container(data)
