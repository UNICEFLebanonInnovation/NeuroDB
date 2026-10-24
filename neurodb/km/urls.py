from django.urls import path

from . import views

app_name = "km"

urlpatterns = [
    path("", views.index, name="index"),
    path("documents/", views.documents, name="documents"),
]
