"""M08.1: file codes against tricky names, and the block list."""

import datetime
import re

import pytest
from ots.files.blocklist import extension_of, is_blocked
from ots.files.codes import clean_extension, make_code, person_part, slugify, unique_code

AT = datetime.datetime(2026, 11, 9, 10, 13, 12, tzinfo=datetime.UTC)  # 15:43:12 in IST
SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def code(**over):
    args = {
        "committed_at": AT,
        "employee_id": "EMP003",
        "full_name": "Rahul Sharma",
        "task_code": "T-2026-000123",
        "title": "Monthly Sales Report",
        "version": 2,
        "original_filename": "Sales.XLSX",
    }
    args.update(over)
    return make_code(**args)


def test_the_example_from_the_plan_is_reproduced_exactly():
    assert code() == "20261109-154312_EMP003-RahulS_T-2026-000123-monthly-sales-report_v2.xlsx"


def test_the_time_is_ist_even_across_midnight():
    late = datetime.datetime(2026, 11, 9, 19, 0, 0, tzinfo=datetime.UTC)  # 00:30 next day in IST
    assert code(committed_at=late).startswith("20261110-003000_")


def test_library_files_use_lib_in_place_of_a_task_code():
    assert "_LIB-policy-handbook_v1." in code(task_code=None, title="Policy Handbook", version=1)


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Monthly Sales Report", "monthly-sales-report"),
        ("  ---Spaced   out---  ", "spaced-out"),
        ("Café Münü Ñandú", "cafe-munu-nandu"),
        ("Q3/Q4: plan & budget (final)!!", "q3-q4-plan-budget-final"),
        ("x" * 100, "x" * 40),
        ("word " * 30, "word-word-word-word-word-word-word-word"),
        ("日本語のタイトル", ""),
        ("../../etc/passwd", "etc-passwd"),
        ("A" * 39 + " bcd", "a" * 39),  # cut at 40 leaves a trailing hyphen, which is removed
        ("", ""),
    ],
)
def test_slug_is_lowercase_ascii_hyphenated_and_at_most_40(title, expected):
    assert slugify(title) == expected
    assert len(slugify(title)) <= 40


def test_a_title_with_no_usable_characters_gets_a_placeholder():
    assert "_T-2026-000123-untitled_v2." in code(title="日本語")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Rahul Sharma", "RahulS"),
        ("rahul sharma", "RahulS"),
        ("Rahul", "Rahul"),
        ("Anna Maria de la Cruz", "AnnaC"),
        ("Zoë Ñandú", "ZoeN"),
        ("  ", "User"),
        ("日本 語", "User"),
        ("O'Brien-Smith Pat", "OBrienSmithP"),
        ("mcDonald Ray", "McDonaldR"),
    ],
)
def test_person_part_is_first_name_plus_last_initial(name, expected):
    assert person_part(name) == expected


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("Sales.XLSX", "xlsx"),
        ("archive.tar.GZ", "gz"),
        ("README", "bin"),
        (".bashrc", "bin"),
        ("trailing.", "bin"),
        ("name.verylongextension", "bin"),
        ("name.exactly10c", "exactly10c"),
        ("C:\\temp\\a.PDF", "pdf"),
        ("dir/sub.v2/file", "bin"),
        ("fichier.é", "e"),
    ],
)
def test_extension_is_lowercased_and_limited(filename, expected):
    assert clean_extension(filename) == expected


@pytest.mark.parametrize(
    "kw",
    [
        {"title": "Ünïcödé ✓ title", "full_name": "Zoë Ñandú", "original_filename": "résumé.PDF"},
        {"title": 'a/b\\c:d*e?f"g<h>i|j', "original_filename": "w;e|ird.na me.txt"},
        {"employee_id": "EMP/00 3"},
        {"task_code": "T-2026-000123 ../x"},
        {"title": "x" * 500, "full_name": "y" * 300},
    ],
)
def test_only_safe_characters_ever_appear(kw):
    result = code(**kw)
    assert SAFE.match(result), result
    assert "/" not in result and "\\" not in result and " " not in result


def test_a_collision_gets_a_2_before_the_extension_then_3():
    base = code()
    taken = {base}
    second = unique_code(base, taken.__contains__)
    assert second == base.replace("_v2.xlsx", "_v2-2.xlsx")
    taken.add(second)
    assert unique_code(base, taken.__contains__) == base.replace("_v2.xlsx", "_v2-3.xlsx")
    assert unique_code("free.txt", taken.__contains__) == "free.txt"


def test_extension_of_matches_the_block_list_rules():
    assert extension_of("Setup.EXE") == "exe"
    assert extension_of("noext") == ""
    assert is_blocked("run.ps1") and is_blocked("A.JAR") and not is_blocked("report.pdf")
