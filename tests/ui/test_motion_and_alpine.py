"""IMP-16.7: motion tokens and reduced-motion support exist; no template uses an Alpine expression the CSP build
cannot run (it silently stops the whole component); the chat keeps DOM nodes out of reactive data."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2] / "app"
SIMPLE = re.compile(r"^!?[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*(\(\))?$")
# `:attr` is a binding only when it stands alone; `x-transition:enter="menu-enter"` holds class names, not an expression
ATTR = re.compile(r'(?:x-show|x-text|x-html|(?<![\w-]):[a-z-]+|x-bind:[a-z-]+)="([^"]*)"')


def templates():
    yield from (ROOT / "templates").rglob("*.html")
    yield from (ROOT / "ots").rglob("templates/**/*.html")


def test_no_template_uses_an_expression_the_alpine_csp_build_cannot_run():
    """Comparisons, `||`, string joins and calls with arguments make Alpine's CSP build stop the component."""
    bad = []
    for path in templates():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for m in ATTR.finditer(line):
                v = m.group(1).strip()
                if "{{" in v or "{%" in v or not v:
                    continue
                if not SIMPLE.match(v):
                    bad.append(f"{path.relative_to(ROOT)}:{n}: {v[:60]}")
    assert not bad, bad


def test_motion_tokens_and_reduced_motion_are_defined():
    css = (ROOT / "static_src/app.css").read_text()
    for token in ("--motion-fast", "--motion-normal", "--motion-smooth", "--motion-slow", "--ease-enter"):
        assert token in css, token
    block = css.split("prefers-reduced-motion: reduce", 1)[1]
    assert ".motion-msg-in" in block and "animation: none" in block


def test_new_message_animation_is_only_added_to_messages_that_arrive_after_load():
    js = (ROOT / "static/js/taskchat.js").read_text()
    assert 'classList.add("motion-msg-in")' in js
    assert (
        js.count("motion-msg-in") == 2
    )  # the optimistic row and a merged-in arrival; never the loaded history
    assert "prefers-reduced-motion" in js  # the script checks the setting before adding it


def test_the_chat_does_not_keep_dom_nodes_on_alpines_reactive_data():
    js = (ROOT / "static/js/taskchat.js").read_text()
    assert "this.log" not in js and "this.root" not in js
    assert "logEl" in js and "data-chat-log" in js


def test_the_page_links_scripts_with_a_version_so_an_update_reaches_browsers():
    base = (ROOT / "templates/base.html").read_text()
    assert "{% static 'js/" not in base and base.count("{% asset 'js/") >= 10
