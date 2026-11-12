from django.urls import path

from . import views

urlpatterns = [
    path("audit", views.audit_list),
    path("audit/export", views.audit_export),
    path("app/people/<uuid:pk>/activity", views.person_activity),
]
