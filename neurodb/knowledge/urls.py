from django.urls import path

from . import views

app_name = "knowledge"

urlpatterns = [
    path("", views.index, name="index"),
    path("add/", views.add, name="add"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/file/", views.download, name="file"),
    path("<int:pk>/reindex/", views.reindex, name="reindex"),
    path("<int:pk>/delete/", views.delete, name="delete"),
]
