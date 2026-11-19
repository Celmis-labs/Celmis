"""A 1C dump is all Cyrillic paths. With git's default ``core.quotepath`` the
diff prints them octal-escaped in quotes, so the incremental pass would look
for files that do not exist and miss every real change."""

from __future__ import annotations

from src.sync.incremental import _git_diff
from tests.kb.conftest import make_config_repo


def test_a_changed_cyrillic_module_comes_back_under_its_real_path(tmp_path):
    first, second = make_config_repo(tmp_path)
    assert _git_diff(tmp_path, first, second) == [("M", "Documents/Заказ/Ext/ObjectModule.bsl")]
