from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from ots.core.errors import NotFound

SHELLS = {"login", "dashboard", "list-detail", "profile", "settings"}


def _allowed(request: HttpRequest) -> bool:
    """Style guide and shells are for developers: DEBUG, or the Super Admin once M02 exists."""
    user = getattr(request, "user", None)
    is_admin = user is not None and user.is_authenticated and getattr(user, "role", "") == "SUPER_ADMIN"
    return bool(settings.DEBUG or is_admin)


def styleguide(request: HttpRequest) -> HttpResponse:
    if not _allowed(request):
        raise NotFound("Not found")
    ctx = {
        "page_title": "Style guide",
        "sg_columns": [
            {"label": "Task"},
            {"label": "Status"},
            {"label": "Due", "align": "right", "sort_url": "#"},
        ],
        "sg_rows": [
            ["Monthly sales report", "Working", "12 Nov"],
            ["Vendor reconciliation", "Waiting", "14 Nov"],
        ],
        "sg_none": [],
        "sg_right": [2],
        # Literal class names so Tailwind's scanner can see them.
        "sg_colors": [
            ("canvas", "bg-canvas"),
            ("ink-900", "bg-ink-900"),
            ("ink-600", "bg-ink-600"),
            ("ink-400", "bg-ink-400"),
            ("line", "bg-line"),
            ("accent", "bg-accent"),
            ("ok", "bg-ok"),
            ("bad", "bg-bad"),
            ("warn", "bg-warn"),
        ],
    }
    return render(request, "styleguide.html", ctx)


def shell(request: HttpRequest, name: str) -> HttpResponse:
    if not _allowed(request) or name not in SHELLS:
        raise NotFound("Not found")
    ctx: dict[str, object] = {"page_title": name.replace("-", " ").title()}
    if name == "dashboard":
        ctx["stats3"] = [("Open tasks", 0), ("Awaiting review", 0), ("Overdue", 0)]
        ctx["stats5"] = [("Assigned", 0), ("Working", 0), ("Waiting", 0), ("Submitted", 0), ("Approved", 0)]
    elif name == "list-detail":
        ctx["cols"] = [{"label": "Item"}, {"label": "Status"}]
        ctx["rows"] = []
    elif name == "profile":
        ctx["initial_tab"] = "work"
        ctx["tabs"] = [
            ("work", "Work"),
            ("attendance", "Attendance"),
            ("files", "Files"),
            ("activity", "Activity"),
        ]
    return render(request, f"shells/{name}.html", ctx)
