from django.urls import path

from . import views

urlpatterns = [
    path("login", views.login_view),
    path("logout", views.logout_view),
    path("password/change", views.change_password_view),
    path("app/people/<uuid:pk>/update", views.person_update),
]
