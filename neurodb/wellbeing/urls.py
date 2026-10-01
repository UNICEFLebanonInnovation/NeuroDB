from django.urls import path

from . import views

app_name = "wellbeing"

urlpatterns = [
    path("", views.summaries, name="summaries"),
    path("flags/", views.flags, name="flags"),
    path("flags/<int:pk>/follow-up/", views.follow_up, name="follow_up"),
]
