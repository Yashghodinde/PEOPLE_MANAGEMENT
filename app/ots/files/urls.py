from django.urls import path

from . import views

urlpatterns = [
    path("files/uploads", views.uploads, name="file-uploads"),
    path("files/uploads/<uuid:upload_id>", views.upload_detail, name="file-upload"),
    path("app/tasks/<uuid:task_id>/files", views.task_files_panel, name="task-files"),
    path("files/v/<uuid:version_id>/download", views.download, name="file-download"),
    path("files/v/<uuid:version_id>/preview", views.preview, name="file-preview"),
    path("files/<uuid:file_id>/trail", views.file_trail, name="file-trail"),
    path("files/v/<uuid:version_id>/handover", views.handover, name="file-handover"),
]
