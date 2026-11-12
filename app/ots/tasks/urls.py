from django.urls import path

from . import views

urlpatterns = [
    path("app/dashboard", views.dashboard_view),
    path("app/tasks", views.task_list),
    path("app/tasks/new", views.task_new_or_create),
    path("app/tasks/<str:task_id>", views.task_detail),
    path("app/tasks/<str:task_id>/update", views.task_update),
    path("app/tasks/<str:task_id>/cancel", views.task_cancel),
    path("app/tasks/<str:task_id>/people", views.task_add_people),
    path("app/tasks/<str:task_id>/comment", views.task_comment),
    path("app/assignments/<str:assignment_id>/<str:action>", views.assignment_action),
]
