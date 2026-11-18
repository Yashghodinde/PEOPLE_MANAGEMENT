from django.http import HttpRequest

from ots.files.nav import files_strip
from ots.notifications.selectors import unread_count

from .nav import items_for


def ui(request: HttpRequest) -> dict[str, object]:
    user = getattr(request, "user", None)
    role = getattr(user, "role", None) if user is not None and user.is_authenticated else None
    return {
        "nav_items": [
            {"item": i, "active": request.path.startswith(i.match or i.path)} for i in items_for(role)
        ],
        "unread_count": unread_count(user),
        "files_nav": files_strip(user, request.path) if role else None,
    }
