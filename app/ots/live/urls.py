from django.urls import path

from . import views

urlpatterns = [path("live/stream", views.stream)]
