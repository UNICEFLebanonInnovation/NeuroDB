from django.urls import path

from . import views

app_name = "cpd"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("indicators/<int:pk>/", views.indicator, name="indicator"),
    path("documents/<int:pk>/", views.document, name="document"),
]
