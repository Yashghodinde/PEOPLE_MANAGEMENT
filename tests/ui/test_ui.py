import re
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import Client
from ots.ui import status

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.django_db


@pytest.fixture
def dev(settings):
    settings.DEBUG = True


def get(path):
    return Client().get(path)


@pytest.mark.parametrize(
    "value,tone",
    [("APPROVED", "ok"), ("ARCHIVED", "ok"), ("SUBMITTED", "warn"), ("BLOCKED", "warn"), ("FAILED", "bad"),
     ("MISSING", "bad"), ("ASSIGNED", "neutral"), ("IN_PROGRESS", "neutral"), ("whatever", "neutral")],
)  # fmt: skip
def test_status_tone_mapping(value, tone):
    assert status.tone(value) == tone


def test_styleguide_hidden_unless_debug_or_super_admin():
    assert get("/styleguide").status_code == 404


def test_styleguide_renders_every_component_with_no_external_urls(dev):
    r = get("/styleguide")
    html = r.content.decode()
    assert r.status_code == 200
    assert not re.findall(r"""(?:src|href)=["']https?://""", html), "no CDN or external asset URLs"
    for marker in (
        "Primary",
        "Off-site",
        "Not yet off-site",
        "3 changes waiting",
        "Load more",
        "No tasks yet.",
        "Confirm action",
        "Day detail",
    ):
        assert marker in html, marker
    assert "rounded-none" not in html


def test_base_layout_structure(dev):
    html = get("/shells/dashboard").content.decode()
    for needle in (
        'aria-label="Main navigation"',
        'id="main"',
        "X-CSRFToken",
        'x-data="sidebar"',
        "toastRegion",
        "vendor/htmx.min.js",
        "vendor/alpine-csp.min.js",
        "Skip to content",
    ):
        assert needle in html, needle


def test_shell_login(dev):
    html = get("/shells/login").content.decode()
    assert "max-w-[377px]" in html and "rounded-full" in html and "Continue" in html and "camera" in html


def test_shell_dashboard_stat_rows(dev):
    html = get("/shells/dashboard").content.decode()
    assert "md:grid-cols-3" in html and "md:grid-cols-5" in html and html.count("shadow-card") >= 9


def test_shell_list_detail_uses_golden_split(dev):
    assert "lg:grid-cols-split" in get("/shells/list-detail").content.decode()


def test_shell_profile_has_four_tabs(dev):
    html = get("/shells/profile").content.decode()
    for tab in ("Work", "Attendance", "Files", "Activity"):
        assert f">{tab}</button>" in html
    assert 'role="tablist"' in html


def test_shell_settings(dev):
    html = get("/shells/settings").content.decode()
    assert "Attendance and selfie policy text" in html and "Pair new display" in html


def test_unknown_shell_404(dev):
    assert get("/shells/nope").status_code == 404


@pytest.mark.parametrize(
    "code,name", [(403, "Access denied"), (404, "Page not found"), (500, "Something went wrong")]
)
def test_error_pages_render(code, name):
    from django.template.loader import render_to_string

    html = render_to_string(f"{code}.html")
    assert name in html and str(code) in html and "Traceback" not in html


def test_error_middleware_page_uses_template_and_escapes():
    from django.test import override_settings

    with override_settings(ROOT_URLCONF="tests.core.urls_test"):
        r = Client().get("/e/xss")
    assert r.status_code == 404 and b"&lt;script&gt;" in r.content and b"Reference:" in r.content


def test_template_lint_passes():
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_templates.py")], capture_output=True, text=True
    )  # noqa: S603
    assert out.returncode == 0, out.stderr


def test_template_lint_catches_violations(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_templates

    bad = tmp_path / "bad.html"
    bad.write_text(
        '<div class="rounded-none" style="color: red"><img src="https://cdn.example.com/x.png">'
        '<p style="x">#FF00FF</p><button onclick="go()">x</button><script>alert(1)</script></div>\n'
    )
    problems = " ".join(check_templates.check(bad))
    for expect in (
        "rounded-none",
        "external asset URL",
        "inline style colour",
        "not a design token",
        "inline <script>",
    ):
        assert expect in problems, expect
    bad.write_text('<a href="#main" class="text-ink-900">ok</a><meta content="#FFFFFF">\n')
    assert check_templates.check(bad) == []
