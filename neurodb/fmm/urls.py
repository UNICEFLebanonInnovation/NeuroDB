"""Monitoring insights (``/fmm/``): the page, its visits table and CSV, the visit page, its review, the
visit look-up and the drill-down window of chart cells and counts. Later steps add the AI brief and
the chat."""

from django.urls import path

from . import views

app_name = "fmm"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("visits/", views.visits, name="visits"),
    path("visits/<slug:key>/", views.visit, name="visit"),
    path("visits/<slug:key>/review/", views.review, name="review"),
    path("lookup/", views.lookup, name="lookup"),
    path("drill/", views.drill, name="drill"),
]
