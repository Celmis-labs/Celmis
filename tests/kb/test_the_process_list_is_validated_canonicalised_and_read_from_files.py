"""A team's list of processes: what is refused, what is normalised, what an upload may be."""

from __future__ import annotations

import json

import pytest

from src.kb import processes
from src.kb.processes import ProcessSpecError, normalise, parse_upload, split_objects


def test_an_id_is_made_from_the_name_and_objects_are_kept_as_written():
    [spec] = normalise([{"name": "Резервування замовлення", "objects": ["Документ.ЗаказКлиента", "Documents.X"]}])
    assert spec.id == "резервування-замовлення"
    assert spec.objects == ["Документ.ЗаказКлиента", "Documents.X"]


def test_objects_in_any_spelling_are_canonical_and_the_rest_are_code_targets():
    [spec] = normalise([{"name": "P", "objects": [
        "Документ.ЗаказКлиента", "Document.ЗаказКлиента", "РегистрНакопления.ТоварыВРезерве",
        "src/orders/reserve.py", "OrderService.reserve"]}])
    canon, other = split_objects(spec)
    assert canon == ["Documents.ЗаказКлиента", "AccumulationRegisters.ТоварыВРезерве"]
    assert other == ["src/orders/reserve.py", "OrderService.reserve"]


def test_objects_may_be_one_string_split_on_separators():
    [spec] = normalise([{"name": "P", "objects": "Документ.А; Документ.Б | Справочник.В\nsrc/x.py"}])
    assert spec.objects == ["Документ.А", "Документ.Б", "Справочник.В", "src/x.py"]


@pytest.mark.parametrize("row, problem", [
    ({"description": "x"}, "name is required"),
    ({"name": "x" * (processes.MAX_NAME + 1)}, "name is longer"),
    ({"name": "p", "description": "x" * (processes.MAX_DESCRIPTION + 1)}, "description is longer"),
    ({"name": "p", "objects": [f"Документ.О{i}" for i in range(processes.MAX_OBJECTS + 1)]}, "more than"),
    ({"name": "p", "id": "../etc"}, "is not usable"),
    ({"name": "p", "id": ".hidden"}, "is not usable"),
    ({"name": "p", "id": "a/b"}, "is not usable"),
    ({"name": "p", "objects": ["x" * (processes.MAX_OBJECT_CHARS + 1)]}, "longer than"),
    ("not a dict", "not an object"),
])
def test_a_wrong_row_refuses_the_whole_list_and_says_which(row, problem):
    with pytest.raises(ProcessSpecError) as err:
        normalise([{"name": "fine"}, row])
    assert any(problem in p and p.startswith("#2") for p in err.value.problems), err.value.problems


def test_a_repeated_id_is_refused_and_so_is_a_list_over_the_cap():
    with pytest.raises(ProcessSpecError, match="already used by #1"):
        normalise([{"name": "Облік"}, {"name": "x", "id": "облік"}])
    with pytest.raises(ProcessSpecError, match=f"at most {processes.MAX_PROCESSES}"):
        normalise([{"name": f"p{i}"} for i in range(processes.MAX_PROCESSES + 1)])


def test_json_yaml_and_csv_uploads_give_the_same_list():
    rows = [{"name": "Резерв", "description": "d", "objects": ["Документ.А", "Справочник.Б"], "domain": "sales"}]
    as_json = json.dumps({"processes": rows}).encode()
    as_yaml = ("processes:\n  - name: Резерв\n    description: d\n    domain: sales\n"
               "    objects:\n      - Документ.А\n      - Справочник.Б\n").encode()
    as_csv = "name;description;objects;domain\nРезерв;d;Документ.А|Справочник.Б;sales\n".encode()
    got = [normalise(parse_upload(n, d)) for n, d in
           (("l.json", as_json), ("l.yaml", as_yaml), ("l.yml", as_yaml), ("l.csv", as_csv))]
    assert all(g == got[0] for g in got)
    assert got[0][0].objects == ["Документ.А", "Справочник.Б"] and got[0][0].domain == "sales"


def test_a_csv_with_a_bom_and_commas_is_read_and_a_bare_list_is_accepted():
    data = "﻿name,objects\nОблік,Документ.А\n".encode()
    assert normalise(parse_upload("x.csv", data))[0].objects == ["Документ.А"]
    assert normalise(parse_upload("x.json", b'[{"name": "A"}]'))[0].id == "a"


@pytest.mark.parametrize("name, data, problem", [
    ("x.txt", b"hi", "use a .json"),
    ("x.json", b"{broken", "cannot read the file"),
    ("x.yaml", b"a: [", "cannot read the file"),
    ("x.json", b'{"a": 1}', "expected a list"),
    ("x.json", b"\xff\xfe", "not UTF-8"),
    ("x.json", b" " * (processes.MAX_UPLOAD_BYTES + 1), "larger than"),
])
def test_an_unreadable_upload_is_refused_with_a_reason(name, data, problem):
    with pytest.raises(ProcessSpecError, match=problem):
        parse_upload(name, data)


def test_the_list_is_saved_whole_and_read_back(tmp_path, monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    get_settings.cache_clear()
    try:
        processes.save("acme", normalise([{"name": "Облік", "objects": ["Документ.А"], "tags": ["a", "b"]}]),
                       by="me@example.invalid")
        processes.save("acme", normalise([{"name": "Інший"}]))
        assert [s.id for s in processes.load("acme")] == ["інший"]
        assert processes.load("nothing-here") == []
    finally:
        get_settings.cache_clear()
