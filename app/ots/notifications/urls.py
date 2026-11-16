from django.urls import path

from . import views

urlpatterns = [
    path("app/notifications", views.bell),
    path("app/notifications/badge", views.badge),
    path("app/notifications/read-all", views.read_all),
    path("app/notifications/<str:notification_id>/open", views.open_notification),
]
