"""WCAG AA contrast for every text/background pair in the token set (M01.1)."""

import json
from pathlib import Path

import pytest

TOKENS = json.loads((Path(__file__).parents[2] / "app/static_src/tokens.json").read_text())["colors"]


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    chan = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chan]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("ink", ["ink-900", "ink-600", "ink-400", "accent", "ok", "bad", "warn"])
def test_text_on_canvas(ink):
    assert ratio(TOKENS[ink], TOKENS["canvas"]) >= 4.5, f"{ink} on canvas"


@pytest.mark.parametrize("bg", ["accent", "ok", "bad", "warn"])
def test_white_text_on_filled_backgrounds(bg):
    assert ratio(TOKENS["canvas"], TOKENS[bg]) >= 4.5, f"white on {bg}"


def test_no_colours_outside_the_token_set():
    assert set(TOKENS) == {"canvas", "ink-900", "ink-600", "ink-400", "line", "accent", "ok", "bad", "warn"}
