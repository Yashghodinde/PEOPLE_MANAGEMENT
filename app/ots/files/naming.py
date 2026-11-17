"""Is this file name meaningful? (IMP-01). Pure: no database, no clock except the date the caller passes in.

A name is meaningful when its stem holds at least one real word: 3 or more letters (any script) that are not a
number, a date, a version label, a placeholder such as "untitled", or keyboard mash such as "asdfgh". Numbers
never help a name pass and never make a good name fail. Generic words (invoice, report, notes) are real words.
"""

from __future__ import annotations

import datetime
import posixpath
import re
import unicodedata
from dataclasses import dataclass

from django.conf import settings

MESSAGE = (
    "This file name does not contain a meaningful description. Please rename it using the date, "
    "company/project name, and subject or work description. Files with names such as Untitled, "
    "Document, or random numbers are not accepted."
)

_SPLIT = re.compile(r"[\s\-_.]+")
_FY = re.compile(r"^fy\d{2,4}$")
_VERSION = re.compile(r"^v\d+$")
_LABELS = {"final", "copy", "new"}
_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
SUGGESTION_MAX = 80


@dataclass(frozen=True)
class NameContext:
    """What the caller knows about where the file is going; only used to build a suggestion."""

    task_code: str = ""
    task_title: str = ""
    client: str = ""
    today: datetime.date | None = None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""  # "" when ok; "empty" or "no_word" otherwise
    message: str = ""
    suggestion: str = ""


def _placeholders() -> set[str]:
    raw = getattr(settings, "OTS_NAME_PLACEHOLDERS", None) or []
    return {unicodedata.normalize("NFKC", p).casefold() for p in raw}


def _min_word() -> int:
    return max(1, int(getattr(settings, "OTS_NAME_MIN_WORD", 3)))


def _is_letter(ch: str) -> bool:
    # Marks count as letters so a Hindi word is not cut at its vowel signs.
    return ch.isalpha() or unicodedata.category(ch).startswith("M")


def _letter_runs(token: str) -> list[str]:
    runs: list[str] = []
    cur: list[str] = []
    for ch in token:
        if _is_letter(ch):
            cur.append(ch)
        elif cur:
            runs.append("".join(cur))
            cur = []
    if cur:
        runs.append("".join(cur))
    return runs


def _is_mash(run: str) -> bool:
    if len(run) < 4 or not run.isascii():
        return False
    if len(set(run)) == 1:
        return True
    return any(run in row or run[::-1] in row for row in _ROWS)


def _split(name: str) -> tuple[str, str]:
    """(stem, extension with its dot) of a plain file name."""
    base = unicodedata.normalize("NFKC", (name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    if base.startswith(".") and base.count(".") == 1:  # ".pdf": nothing but an extension
        return "", base
    return posixpath.splitext(base)


def _words(stem: str) -> list[str]:
    """The real words in a stem, original spelling kept; empty when the name says nothing."""
    placeholders, min_word = _placeholders(), _min_word()
    out: list[str] = []
    for token in _SPLIT.split(stem):
        low = token.casefold()
        if not low or low.isdigit() or _FY.match(low) or _VERSION.match(low) or low in _LABELS:
            continue
        if low in placeholders:
            continue
        for run in _letter_runs(token):
            r = run.casefold()
            if len(run) >= min_word and r not in placeholders and r not in _LABELS and not _is_mash(r):
                out.append(run)
    return out


def _hyphenate(text: str) -> str:
    parts = [p for p in re.split(r"[^\w]+", unicodedata.normalize("NFKC", text or ""), flags=re.UNICODE) if p]
    return "-".join(p[:1].upper() + p[1:] for p in parts)


def suggest(name: str, context: NameContext | None = None) -> str:
    """`YYYY-MM-DD_<Context>_<Cleaned-stem>.ext`; only an offer. A name with nothing to keep gets the date and
    context with the subject left for the person to type (so accepting it unchanged still fails the rule)."""
    ctx = context or NameContext()
    stem, ext = _split(name)
    today = ctx.today or datetime.date.today()
    bits = [today.isoformat()]
    for piece in (ctx.client, ctx.task_title or ctx.task_code):
        h = _hyphenate(piece)
        if h:
            bits.append(h)
    kept = _hyphenate(" ".join(_words(stem)))
    if kept and kept.casefold() not in " ".join(bits).casefold():
        bits.append(kept)
    cap = SUGGESTION_MAX - len(ext)
    body = "_".join(bits)[:cap].rstrip("-_")
    return f"{body}_{ext}" if not kept and len(bits) == 1 else f"{body}{ext}"


def judge(name: str, context: NameContext | None = None) -> Verdict:
    stem, _ext = _split(name)
    if not stem.strip():
        return Verdict(False, "empty", MESSAGE, suggest(name, context))
    if _words(stem):
        return Verdict(True)
    return Verdict(False, "no_word", MESSAGE, suggest(name, context))
