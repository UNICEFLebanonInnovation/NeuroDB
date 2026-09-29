from django.urls import path

from . import views

app_name = "education"

urlpatterns = [
    path("", views.index, name="dashboard"),
    path("makani/", views.makani_page, name="makani"),
    path("dirasa/", views.dirasa_page, name="dirasa"),
]
