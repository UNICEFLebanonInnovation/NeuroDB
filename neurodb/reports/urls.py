from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.home, name="overview"),
    path("databases/<int:pk>/", views.database_dashboard, name="database_dashboard"),
    path("databases/<int:pk>/analytical/", views.database_analytical, name="database_analytical"),
    path("databases/<int:pk>/snapshot/", views.database_snapshot, name="database_snapshot"),
    path("databases/<int:pk>/map/", views.database_map, name="database_map"),
    path("databases/<int:pk>/raw-data.<str:fmt>", views.database_raw_data, name="database_raw_data"),
    path("databases/<int:pk>/indicators/<int:master_id>/", views.indicator_detail, name="indicator_detail"),
    path("reports/<int:pk>/", views.report_dashboard, name="report_dashboard"),
    path("reports/<int:pk>/analytical/", views.report_analytical, name="report_analytical"),
    path("reports/<int:pk>/hpm/", views.report_hpm, name="report_hpm"),
    path("programmes/", views.programmes, name="programmes"),
    path("programmes/summary/", views.programme_summary, name="programme_summary"),
    path("programmes/<int:pk>/detail/", views.programme_detail, name="programme_detail"),
    path("donors/", views.donors, name="donors"),
    path("partners/", views.partners, name="partners"),
    path("partners/<int:pk>/", views.partner_profile, name="partner_profile"),
    path("population/", views.population, name="population"),
    path("library/", views.library, name="library"),
    path("library/<int:pk>/", views.library_item, name="library_item"),
    path("library/<int:pk>/download/", views.library_download, name="library_download"),
    path("library/<int:pk>/file/", views.library_file, name="library_file"),
    path("library/<int:pk>/cover/", views.library_cover, name="library_cover"),
    path("maps/", views.maps, name="maps"),
    path("maps/<int:pk>/", views.map_item, name="map_item"),
    path("data/health/", views.data_health, name="data_health"),
    path("search/", views.search, name="search"),
    path("views/save/", views.saved_view_save, name="saved_view_save"),
    path("views/<int:pk>/delete/", views.saved_view_delete, name="saved_view_delete"),
    path("exports/etools-locations.xlsx", views.export_etools_locations, name="export_etools_locations"),
]
