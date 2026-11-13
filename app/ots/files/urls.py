from django.urls import path

from . import views

urlpatterns = [
    path("files/uploads", views.uploads, name="file-uploads"),
    path("files/uploads/<uuid:upload_id>", views.upload_detail, name="file-upload"),
    path("app/tasks/<uuid:task_id>/files", views.task_files_panel, name="task-files"),
]
