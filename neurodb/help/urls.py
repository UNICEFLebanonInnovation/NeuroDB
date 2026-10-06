from django.urls import path

from . import views

app_name = "help"

urlpatterns = [
    path("", views.index, name="index"),
    path("stream/", views.stream, name="stream"),
    path("panel/", views.panel, name="panel"),
    path("<slug:key>/", views.page, name="page"),
]
