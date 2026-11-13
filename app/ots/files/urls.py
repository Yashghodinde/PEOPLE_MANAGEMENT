from django.urls import path

from . import views

urlpatterns = [
    path("files/uploads", views.uploads, name="file-uploads"),
    path("files/uploads/<uuid:upload_id>", views.upload_detail, name="file-upload"),
]
