"""IMP-01.1: the file-name rule. Table of names, Unicode, long names, and the 'digits never decide' property."""

import datetime
import random

import pytest
from ots.files import naming
from ots.files.naming import MESSAGE, NameContext, judge, suggest

TODAY = datetime.date(2026, 11, 17)
CTX = NameContext(
    task_code="T-2026-000123", task_title="Income tax working", client="AZ Exports", today=TODAY
)

REJECTED = [
    # the spec's own examples
    "Untitled.pdf", "Untitled 1.docx", "Document.docx", "New File.xlsx", "123456.pdf", "1234567890.pdf",
    "1 2 3 4 5.pdf", "asdfgh.pdf", "qwerty.xlsx",
    # more of the same kinds
    "untitled.PDF", "UNTITLED (2).pdf", "Document1.docx", "doc.docx", "file.txt", "newfile.xlsx", "scan.pdf",
    "scanned.pdf", "Scan 0001.pdf", "image.png", "IMG_1234.jpg", "img-20261117.jpg", "photo.jpg",
    "Screenshot 2026-11-17 at 1.19.07 PM.png", "download.pdf", "Sheet1.xlsx", "Book1.xlsx", "page.pdf", "temp.pdf",
    "test.pdf", "2026.pdf", "2026-11-17.pdf", "17-11-2026.pdf", "FY2026.pdf", "FY26.xlsx", "v1.docx", "final.pdf",
    "final copy.pdf", "copy.docx", "zxcvbn.pdf", "aaaaaa.pdf", "uiop.pdf", "---.pdf", "...", "_ _ _.pdf", ".pdf",
    "   .pdf", "ab.pdf", "a b c d.pdf", "Untitled_final_v2_2026.docx",
]  # fmt: skip

ACCEPTED = [
    "AZ-Exports_Invoice_123456.pdf", "2026-11-17_AZ-Exports_GST-Details.pdf",
    "2026-11-17_Prop-Organic_Income-Tax-Working.xlsx", "2026-11-17_Internal_Employee-Attendance-Report.xlsx",
    "2026-11-17_Office_Equipment-Purchase-Invoice.pdf", "invoice.pdf", "report.docx", "statement.pdf",
    "details.xlsx", "notes.txt", "Invoice 2026.pdf", "GST Return FY2026.pdf", "salary_slip_oct.pdf",
    "Café menu.pdf", "résumé.docx", "रिपोर्ट.pdf", "बिल अक्टूबर.xlsx", "請求書.pdf", "Müller-Angebot.pdf",
    "archive.tar.gz", "Test Invoice.pdf", "Untitled but Meaningful Budget.pdf", "q3 budget.xlsx",
    "abc.pdf",  # three letters is the shortest word
]  # fmt: skip


@pytest.mark.parametrize("name", REJECTED)
def test_meaningless_names_are_refused_with_the_exact_message(name):
    v = judge(name)
    assert not v.ok and v.message == MESSAGE and v.reason in {"empty", "no_word"}


@pytest.mark.parametrize("name", ACCEPTED)
def test_meaningful_names_are_accepted(name):
    v = judge(name)
    assert v.ok and v.message == "" and v.suggestion == ""


def test_the_table_has_the_required_breadth():
    assert len(REJECTED) + len(ACCEPTED) >= 40


def test_numbers_alone_never_decide_either_way():
    rng = random.Random(7)  # noqa: S311  (a fixed seed for repeatable test input, not security)
    for name in REJECTED + ACCEPTED:
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            continue
        before = judge(name).ok
        for _ in range(5):
            digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 9)))
            sep = rng.choice([" ", "-", "_", "."])
            for varied in (f"{digits}{sep}{stem}.{ext}", f"{stem}{sep}{digits}.{ext}"):
                assert judge(varied).ok == before, varied


def test_a_year_or_fiscal_year_alone_is_not_a_description():
    for n in ("2026.pdf", "FY2026.pdf", "FY26.pdf", "2026-27.pdf", "2026-11-17.xlsx", "17-11-2026.xlsx"):
        assert not judge(n).ok


def test_very_long_names_are_judged_on_their_words_and_suggestions_are_capped():
    assert judge(("quarterly-" * 40) + "report.pdf").ok
    assert not judge(("1234 " * 60) + ".pdf").ok
    long_ctx = NameContext(task_title="x" * 300, client="y" * 300, today=TODAY)
    assert len(suggest("Untitled.pdf", long_ctx)) <= naming.SUGGESTION_MAX


def test_placeholder_list_and_word_length_come_from_settings(settings):
    settings.OTS_NAME_PLACEHOLDERS = ["draft"]
    assert not judge("draft.pdf").ok and judge("untitled letter.pdf").ok
    settings.OTS_NAME_MIN_WORD = 5
    assert not judge("gst.pdf").ok and judge("returns.pdf").ok


def test_suggestion_uses_date_client_and_task_and_keeps_the_extension():
    s = suggest("Untitled.pdf", CTX)
    assert s == "2026-11-17_AZ-Exports_Income-Tax-Working.pdf" and judge(s).ok
    assert suggest("scan.XLSX", NameContext(task_title="Payroll", today=TODAY)).endswith("_Payroll.XLSX")


def test_a_cleaned_stem_is_kept_in_the_suggestion():
    s = suggest("IMG_0042 gst details.jpg", NameContext(client="Prop Organic", today=TODAY))
    assert s == "2026-11-17_Prop-Organic_Gst-Details.jpg"


def test_without_context_the_suggestion_leaves_the_subject_blank_so_it_still_has_to_be_typed():
    s = suggest("Untitled.pdf", NameContext(today=TODAY))
    assert s == "2026-11-17_.pdf" and not judge(s).ok


def test_the_original_name_is_not_changed_by_judging():
    name = "  Weird   Name .PDF "
    judge(name)
    assert name == "  Weird   Name .PDF "
