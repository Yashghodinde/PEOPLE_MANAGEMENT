from django.urls import path

from . import views

urlpatterns = [
    path("login/selfie", views.selfie_view),
    path("attendance/idle", views.idle_view),
    path("attendance/mode", views.mode_view),
    path("app/attendance", views.my_attendance),
    path("app/attendance/corrections", views.corrections_page),
    path("app/attendance/corrections/<str:correction_id>/approve", views.correction_approve),
    path("app/attendance/holidays", views.holidays_page),
    path("app/attendance/selfie/<str:session_id>", views.selfie_image),
    path("app/attendance/sessions/<str:session_id>/force-logout", views.force_logout),
    path("app/attendance/<str:user_id>", views.attendance_page),
    path("app/attendance/<str:user_id>/correction", views.correction_request),
]
