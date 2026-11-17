from django.urls import path

from . import display_views, views

urlpatterns = [
    path("live/stream", views.stream),
    path("display", display_views.board_page),
    path("display/pair", display_views.pair_page),
    path("display/board", display_views.board_fragment),
    path("app/displays", display_views.displays_admin),
    path("app/displays/code", display_views.displays_code),
    path("app/displays/<str:device_id>/revoke", display_views.displays_revoke),
]
