from django.urls import include, path
from ots.core.health import healthz
from ots.ui import views as ui_views

urlpatterns = [
    path("healthz", healthz),
    path("", include("ots.accounts.urls")),
    path("styleguide", ui_views.styleguide),
    path("shells/<str:name>", ui_views.shell),
]
