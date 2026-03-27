from rest_framework import routers
from django.urls import include, path

from . import views

app_name = 'locations'

api = routers.SimpleRouter()

api.register(r'locations', views.LocationsViewSet, basename='locations')
api.register(r'locations-light', views.LocationsLightViewSet, basename='locations-light')

urlpatterns = [
    path('', include(api.urls)),
    path(
        'locations/pcode/(?P<p_code>\w+)/$', views.LocationsViewSet.as_view({'get': 'retrieve'}),
        name='locations_detail_pcode'
    ),
    path('autocomplete/$', views.LocationQuerySetView.as_view(), name='locations_autocomplete'),
]
