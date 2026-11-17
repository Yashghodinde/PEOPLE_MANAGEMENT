from django.urls import path

from . import views

urlpatterns = [
    path("app/dashboard", views.dashboard_view),
    path("app/receipts/ack", views.receipts_ack),
    path("app/receipts/pending", views.receipts_pending),
    path("app/day-close", views.day_close),
    path("app/day-resume", views.resume_all),
    path("app/tasks", views.task_landing),
    path("app/tasks/all", views.task_list),  # the flat table: filters, search, paging
    path("app/tasks/new", views.task_new_or_create),
    path("app/tasks/by/<str:user_id>", views.task_group_by),  # given to me by that person
    path("app/tasks/to/<str:user_id>", views.task_group_to),  # given by me to that person
    path("app/tasks/<str:task_id>", views.task_detail),
    path("app/tasks/<str:task_id>/update", views.task_update),
    path("app/tasks/<str:task_id>/cancel", views.task_cancel),
    path("app/tasks/<str:task_id>/people", views.task_add_people),
    path("app/tasks/<str:task_id>/comment", views.task_comment),
    path("app/assignments/<str:assignment_id>/<str:action>", views.assignment_action),
]
