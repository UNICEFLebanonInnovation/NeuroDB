from django.urls import path

from . import api

app_name = "api"

urlpatterns = [
    path("databases/<int:pk>/dashboard/", api.DashboardAPI.as_view(), name="dashboard"),
    path("databases/<int:pk>/analytical/", api.AnalyticalAPI.as_view(), name="analytical"),
    path("databases/<int:pk>/map/", api.MapAPI.as_view(), name="map"),
    path(
        "databases/<int:pk>/indicators/<int:master_id>/",
        api.IndicatorDetailAPI.as_view(),
        name="indicator_detail",
    ),
    path("reports/<int:pk>/hpm/", api.HPMAPI.as_view(), name="hpm"),
    path("reports/<int:pk>/analytical/", api.ReportAnalyticalAPI.as_view(), name="report_analytical"),
    path("programmes/", api.ProgrammesAPI.as_view(), name="programmes"),
    path("donors/", api.DonorsAPI.as_view(), name="donors"),
    path("saved-views/", api.SavedViewsAPI.as_view(), name="saved_views"),
    path("saved-views/<int:pk>/", api.SavedViewDetailAPI.as_view(), name="saved_view_detail"),
    path("health/", api.HealthAPI.as_view(), name="health"),
]
