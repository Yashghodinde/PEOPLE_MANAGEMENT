from django.urls import path

from . import people, views

urlpatterns = [
    path("login", views.login_view),
    path("logout", views.logout_view),
    path("password/change", views.change_password_view),
    path("", views.home),
    path("app/people", people.people_list),
    path("app/people/new", people.person_new),
    path("app/people/manager-options", people.manager_options),
    path("app/people/<uuid:pk>", people.person_profile),
    path("app/people/<uuid:pk>/edit", people.person_edit),
    path("app/people/<uuid:pk>/update", people.person_update),
    path("app/people/<uuid:pk>/deactivate", people.person_deactivate),
    path("app/people/<uuid:pk>/reactivate", people.person_reactivate),
    path("app/people/<uuid:pk>/reset-password", people.person_reset_password),
    path("app/org", people.org_chart),
    path("app/org/children/<uuid:pk>", people.org_children),
]
