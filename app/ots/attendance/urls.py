from django.urls import path

from . import views

urlpatterns = [
    path("login/selfie", views.selfie_view),
    path("attendance/idle", views.idle_view),
    path("attendance/mode", views.mode_view),
]
