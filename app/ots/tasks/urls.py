from django.urls import path

from . import views

urlpatterns = [
    path("app/tasks/new", views.task_create),
]
