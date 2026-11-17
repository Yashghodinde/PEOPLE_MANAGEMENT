from django.urls import path

from . import views

urlpatterns = [
    path("files/check-name", views.check_name, name="file-check-name"),
    path("files/uploads", views.uploads, name="file-uploads"),
    path("files/uploads/<uuid:upload_id>", views.upload_detail, name="file-upload"),
    path("app/tasks/<uuid:task_id>/files", views.task_files_panel, name="task-files"),
    path("files/v/<uuid:version_id>/download", views.download, name="file-download"),
    path("files/v/<uuid:version_id>/preview", views.preview, name="file-preview"),
    path("files/search", views.search, name="file-search"),
    path("files/mine", views.mine, name="file-mine"),
    path("files/mine/<str:day>", views.mine_day, name="file-mine-day"),
    path("files/shared", views.shared, name="file-shared"),
    path("files/deleted", views.deleted, name="file-deleted"),
    path("files/<uuid:file_id>/rename", views.personal_rename, name="file-rename"),
    path("files/<uuid:file_id>/hide", views.personal_hide, name="file-hide"),
    path("files/<uuid:file_id>/restore", views.personal_restore, name="file-restore"),
    path("files/library", views.library_page, name="file-library"),
    path("files/library/folders", views.library_folder, name="file-library-folder"),
    path("files/library/files/<uuid:file_id>/move", views.library_move, name="file-library-move"),
    path("files/attach/<uuid:task_id>", views.attach, name="file-attach"),
    path("files/<uuid:file_id>/trail", views.file_trail, name="file-trail"),
    path("files/v/<uuid:version_id>/handover", views.handover, name="file-handover"),
]
