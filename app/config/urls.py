from django.urls import path
from ots.core.health import healthz

urlpatterns = [path("healthz", healthz)]
