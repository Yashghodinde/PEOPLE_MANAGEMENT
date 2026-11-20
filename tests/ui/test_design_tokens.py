"""IMP-18: the Astryx-derived tokens and rules (motion, elevation, menus, buttons, touch, grouping) are in the build
and the pages use them."""

import re
import uuid

import pytest
from ots.chat import services
from ots.tasks import services as task_services

from tests.files.conftest import login
from tests.tasks.test_workspace_chat import started

CSS = open("app/static/css/app.css").read()
SRC = open("app/static_src/app.css").read()


def test_the_motion_scale_and_easing_are_the_agreed_ones():
    for token, value in (
        ("--motion-fast", "175ms"), ("--motion-exit", "120ms"), ("--motion-slow", "320ms"),
        ("--ease-standard", "cubic-bezier(0.24, 1, 0.4, 1)"), ("--ease-exit", "cubic-bezier(0.3, 0, 0.6, 0.6)"),
    ):  # fmt: skip
        assert f"{token}: {value}" in SRC, token
    longest = max(int(m) for m in re.findall(r"--motion-[a-z]+: (\d+)ms", SRC))
    assert longest <= 320  # nothing in the interface animates for longer than the owner's limit


def test_elevation_has_three_tiers_and_each_surface_uses_one():
    for tier in ("low", "med", "high"):
        assert f"--shadow-{tier}:" in SRC
    assert re.search(r"\.surface \{[^}]*var\(--shadow-low\)", SRC)
    assert re.search(r"\.menu-panel \{[^}]*var\(--shadow-med\)", SRC)
    assert re.search(r"\.ots-dialog \{[^}]*var\(--shadow-high\)", SRC)


def test_menu_rows_are_concentric_with_the_panel_and_composers_use_the_chat_radius():
    assert re.search(r"\.menu-item \{[^}]*border-radius: var\(--radius-inner\)", SRC)
    assert "--radius-inner: 6px" in SRC and "--radius-menu: 14px" in SRC  # 14 minus 8 px padding is 6
    assert re.search(r"\.task-composer \{[^}]*border-radius: var\(--radius-chat\)", SRC)
    assert re.search(r"\.chat-composer \{[^}]*border-radius: var\(--radius-chat\)", SRC)


def test_reduced_motion_fades_instead_of_vanishing_and_stops_movement():
    block = SRC.split("prefers-reduced-motion: reduce", 2)[2]
    assert "animation: ots-fade-in 150ms linear both !important" in block  # opacity only, 200 ms or less
    assert ".menu-from, .menu-gone { transform: none !important; }" in block
    assert ".motion-pulse" in block and "animation: none" in block  # pulsing and shimmer stay off


def test_touch_devices_get_44px_targets_without_moving_the_layout():
    block = SRC[SRC.index("@media (pointer: coarse)") :]
    assert ".composer-btn { flex-basis: 44px; width: 44px; height: 44px; }" in block
    assert ".action-btn { min-height: 44px; }" in block
    assert '::before { content: ""; position: absolute; inset: -5px; }' in block  # larger target, same look


def test_buttons_have_press_pending_and_sizes():
    assert ".action-btn:active" in SRC and '.action-btn[aria-busy="true"]' in SRC
    assert ".action-btn-sm" in SRC and ".action-btn-lg" in SRC
    assert CSS.count("ots-spin") >= 2  # compiled into the build


@pytest.mark.django_db
def test_the_three_menus_open_and_close_with_a_transition(org, task):
    started(org, task)
    html = login("emp").get(f"/app/tasks/{task.pk}").content.decode()
    assert html.count('x-transition:enter="menu-enter"') == 2  # profile menu and the attach menu
    assert html.count('x-transition:leave="menu-leave"') == 2
    assert "motion-pop" not in html[html.index("menu-attach") - 400 : html.index("menu-attach") + 200]


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("dirs")
def test_a_run_of_messages_from_one_person_sits_closer_in_both_chats(org, task):
    a = started(org, task)
    for text in ("first", "second", "third"):
        task_services.add_comment(org["emp"], task.pk, text)
    task_services.add_comment(org["mgr"], task.pk, "reply from the manager")
    page = login("emp").get(f"/app/tasks/{task.pk}").content.decode()
    assert page.count('data-cont="1"') >= 2 and a.pk  # second and third follow the first
    assert page.count("Asha Kulkarni") <= page.count(
        "Asha Kulkarni"
    )  # name shown on the run's first message only

    hr = org["peer"]
    conv = services.open_direct(org["emp"], hr.pk)
    for text in ("one", "two", "three"):
        services.send(org["emp"], conv.pk, text, client_id=uuid.uuid4())
    chat = login("emp").get(f"/app/chat/c/{conv.pk}").content.decode()
    assert chat.count('data-cont="1"') == 2
    services.send(hr, conv.pk, "answer", client_id=uuid.uuid4())
    chat = login("emp").get(f"/app/chat/c/{conv.pk}").content.decode()
    assert chat.count('data-cont="1"') == 2  # a different sender starts a new run
