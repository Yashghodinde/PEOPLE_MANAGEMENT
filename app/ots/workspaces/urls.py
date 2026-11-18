from django.urls import path

from . import views

urlpatterns = [
    path("files/ids", views.ids_list, name="fileid-list"),
    path("files/ids/new", views.ids_create, name="fileid-create"),
    path("files/ids/check", views.ids_check, name="fileid-check"),
    path("files/ids/<uuid:ws_id>", views.ids_detail, name="fileid-detail"),
    path("files/ids/<uuid:ws_id>/f/<uuid:folder_id>", views.ids_folder, name="fileid-folder"),
    path("files/ids/<uuid:ws_id>/folders", views.ids_folder_create, name="fileid-folder-create"),
    path(
        "files/ids/<uuid:ws_id>/folders/<uuid:folder_id>/rename",
        views.ids_folder_rename,
        name="fileid-folder-rename",
    ),
    path("files/ids/<uuid:ws_id>/move", views.ids_move, name="fileid-move"),
    path("files/ids/<uuid:ws_id>/edit", views.ids_edit, name="fileid-edit"),
    path("files/ids/<uuid:ws_id>/members", views.ids_member, name="fileid-member"),
    path("files/ids/<uuid:ws_id>/status", views.ids_status, name="fileid-status"),
    path("files/ids/<uuid:ws_id>/documents/<uuid:file_id>/remove", views.ids_unlink, name="fileid-unlink"),
]
