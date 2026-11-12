from django.urls import path

from . import views

urlpatterns = [path("login/selfie", views.selfie_view)]
