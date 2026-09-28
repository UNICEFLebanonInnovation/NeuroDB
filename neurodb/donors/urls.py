from django.urls import path

from . import views

app_name = "donors"

urlpatterns = [
    path("", views.page, name="page"),
]
