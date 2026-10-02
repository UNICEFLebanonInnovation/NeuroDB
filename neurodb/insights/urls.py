from django.urls import path

from . import views

app_name = "insights"

urlpatterns = [path("forecasts/", views.forecasts, name="forecasts")]
