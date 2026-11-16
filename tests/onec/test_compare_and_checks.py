"""The external report against the synthetic `Акме` configuration.

The file is an older copy with one of each problem (see `tests.onec.acme`):
the comparison has to call it older and name the change it lacks, the checks
have to find every planted problem — and nothing on the lines that are fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.onec.checks import check
from src.onec.compare import compare
from src.onec.config import Config, DirSource
from src.onec.external import inspect_bytes
from src.onec.report import EXTERNAL_MARK, STATIC_MARK, render
from tests.onec import acme

OBJ = f"Reports/{acme.REPORT}/Ext/ObjectModule.bsl"


@pytest.fixture(params=[False, True], ids=["nfc", "nfd"])
def cfg(request, tmp_path: Path) -> Config:
    """The dump as written, and as a macOS copy spells its folder names."""
    return Config(DirSource(acme.write_dump(tmp_path, nfd=request.param)), "acme")


@pytest.fixture(scope="module")
def ext():
    return inspect_bytes(acme.report_file(), f"{acme.REPORT}.erf")


def test_configuration_is_recognised(cfg):
    assert cfg.ok
    assert cfg.find("Reports", acme.REPORT.lower()) == acme.REPORT
    assert cfg.find("CommonModules", acme.MODULE) == acme.MODULE


def test_the_file_is_older_and_lacks_a_change(ext, cfg):
    c = compare(ext, cfg)
    assert c.counterpart == f"Reports.{acme.REPORT}"
    assert c.verdict == "older than the configuration"
    assert [m.id for m in c.missing_markers] == ["ACME-205"]
    assert not c.extra_markers
    obj = c.modules[0]
    kinds = {ch.name: ch.kind for ch in obj.changes}
    assert kinds["Проверки"] == "added"
    assert kinds["ТолькоВКонфигурации"] == "removed"
    assert kinds["Сформировать"] == "changed"
    form = next(m for m in c.modules if "ФормаОтчета" in m.label)
    assert {ch.name: ch.kind for ch in form.changes} == {"ПриЗакрытии": "removed"}


def test_an_identical_copy_is_the_same(tmp_path: Path):
    from tests.onec.container_writer import external

    same = inspect_bytes(external(acme.REPORT, object_module=acme.CONFIG_OBJECT_MODULE,
                                  forms=[("ФормаОтчета", ["ПриОткрытии", "ПриЗакрытии"],
                                          acme.CONFIG_FORM_MODULE)],
                                  templates=[("Макет", ["Шапка"], [])]), "same.erf")
    c = compare(same, Config(DirSource(acme.write_dump(tmp_path)), "acme"))
    assert c.verdict == "same code as the configuration", c.verdict
    assert not c.missing_markers


EXPECTED_ERRORS = {
    7: "Если block(s) not closed",
    8: "ФорматСуммы: passes 3 argument(s), ФорматСуммы takes at most 2",
    9: "Внутренняя: the method is not exported",
    10: "НетТакого: no such method in common module",
    11: "Отменен: the enum has no such value",
    13: "has no Catalog named СкладыАкме",
    19: "НоменклатураАкме.Товар: no predefined item of that name",
    23: "ТоварыНаСкладахСтарый: the configuration has no such object",
    26: "no template named НетМакета",
    31: "Местная: passes 2 argument(s), Местная takes at most 1",
}
EXPECTED_WARNINGS = {
    18: "Заказ.Менеджер",
    28: "area Подвал",
    30: "Лишний",
}


def test_every_planted_problem_is_found(ext, cfg):
    r = check(ext, cfg)
    errors = {f.line: f.message for f in r.findings if f.level == "error" and f.path == OBJ}
    warnings = {f.line: f.message for f in r.findings if f.level == "warning" and f.path == OBJ}
    for line, text in EXPECTED_ERRORS.items():
        assert text in errors.get(line, ""), (line, errors.get(line))
    for line, text in EXPECTED_WARNINGS.items():
        assert text in warnings.get(line, ""), (line, warnings.get(line))


def test_nothing_on_the_lines_that_are_fine(ext, cfg):
    r = check(ext, cfg)
    flagged = {f.line for f in r.findings if f.path == OBJ}
    # The valid call, the known enum value, the existing catalog, the existing
    # attribute, the existing predefined item, the existing document, the
    # existing template, area and parameter.
    for line in (4, 12, 14, 17, 20, 22, 25, 27, 29):
        assert line not in flagged, (line, [f.message for f in r.findings if f.line == line])
    assert len([f for f in r.findings if f.path == OBJ and f.level == "error"]) == len(EXPECTED_ERRORS)


def test_an_unbound_handler_is_an_error_of_the_file(ext, cfg):
    r = check(ext, cfg)
    handler = [f for f in r.findings if "handler ПриЗакрытии" in f.message]
    assert len(handler) == 1
    assert handler[0].level == "error" and not handler[0].pre_existing


def test_without_a_configuration_only_the_file_is_checked(ext):
    r = check(ext, None)
    messages = " ".join(f.message for f in r.findings)
    assert "Если block(s) not closed" in messages
    assert "Местная: passes 2" in messages
    assert "СкладыАкме" not in messages  # needs the configuration
    assert r.skipped


def test_the_report_says_what_it_is(ext, cfg):
    text = render(ext, compare(ext, cfg), check(ext, cfg))
    lines = text.splitlines()
    assert lines[0].startswith(EXTERNAL_MARK)
    assert lines[1] == STATIC_MARK
    assert "verdict: older than the configuration" in text
    assert "ACME-205 2025-05-12" in text
    assert "not checked yet: managed forms" in text
    assert "file line 0" not in text
