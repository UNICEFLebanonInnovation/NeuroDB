"""Monitoring insights (``/fmm/``): the page, its visits table and CSV, the visit page, its review, the
visit look-up, the drill-down window of chart cells and counts, the AI brief (its card, and what was
sent for it), Chat with Data (the streamed answer of a question) and the exports of a filter (the Excel
workbook, the printable report and the Power BI package). The Power BI live feed is at
``/powerbi/fmm/`` (``fmm.powerbi``, in the site's URLs)."""

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
    path("export.xlsx", views.export_xlsx, name="export_xlsx"),
    path("export-powerbi.zip", views.export_powerbi, name="export_powerbi"),
    path("report/", views.report, name="report"),
]
