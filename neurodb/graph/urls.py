from django.urls import path

from . import views

app_name = "graph"

urlpatterns = [
    path("", views.whats_new, name="whats_new"),
    path("email/", views.email, name="email"),
]
