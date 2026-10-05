"""Monitoring insights (``/fmm/``): the page, its visits table and CSV, the visit page, its review, the
visit look-up, the drill-down window of chart cells and counts, the AI brief (its card, and what was
sent for it) and Chat with Data (the streamed answer of a question)."""

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
    path("insights/", views.insights, name="insights"),
    path("insights/<int:pk>/sent/", views.insight_sent, name="insight_sent"),
    path("chat/stream/", views.chat_stream, name="chat_stream"),
]
