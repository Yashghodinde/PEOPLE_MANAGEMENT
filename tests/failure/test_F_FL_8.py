"""F-FL-8: blocked file type. Programs that run on a double click are refused, whatever the case or path, and
only a deliberate Super Admin override (audited, in the upload service from M08.2) lets one through."""

import pytest
from ots.files.blocklist import BLOCKED_EXTENSIONS, is_blocked


def test_the_block_list_is_exactly_the_planned_set():
    assert BLOCKED_EXTENSIONS == {"exe", "msi", "bat", "cmd", "ps1", "vbs", "js", "jar", "scr", "lnk", "com"}


@pytest.mark.parametrize("ext", sorted(BLOCKED_EXTENSIONS))
@pytest.mark.parametrize(
    "fmt", ["file.{e}", "FILE.{E}", "my.report.final.{e}", "C:\\Users\\x\\a.{e}", "a/b/c.{e}"]
)
def test_every_blocked_type_is_refused_in_any_spelling(ext, fmt):
    assert is_blocked(fmt.format(e=ext, E=ext.upper()))


@pytest.mark.parametrize(
    "name",
    ["report.pdf", "sheet.xlsx", "photo.jpg", "notes.txt", "jsfile", "script.json", "exe", "x.exe.txt"],
)
def test_ordinary_files_and_lookalikes_are_allowed(name):
    assert not is_blocked(name)
