from django.http import HttpRequest

from .nav import items_for


def ui(request: HttpRequest) -> dict[str, object]:
    user = getattr(request, "user", None)
    role = getattr(user, "role", None) if user is not None and user.is_authenticated else None
    return {
        "nav_items": [
            {"item": i, "active": i.path != "#" and request.path.startswith(i.path)} for i in items_for(role)
        ]
    }
