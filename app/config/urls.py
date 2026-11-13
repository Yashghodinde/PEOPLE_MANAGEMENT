from django.urls import include, path
from ots.core.health import healthz
from ots.ui import views as ui_views

urlpatterns = [
    path("healthz", healthz),
    path("", include("ots.accounts.urls")),
    path("", include("ots.audit.urls")),
    path("", include("ots.tasks.urls")),
    path("", include("ots.notifications.urls")),
    path("", include("ots.attendance.urls")),
    path("", include("ots.live.urls")),
    path("", include("ots.files.urls")),
    path("styleguide", ui_views.styleguide),
    path("shells/<str:name>", ui_views.shell),
]
