from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
    path("", views.ask, name="ask"),
    path("stream/", views.ask_stream, name="stream"),
]
