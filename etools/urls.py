from django.urls import path

from . import views

urlpatterns = [
    path('partner-profile/', view=views.PartnerProfileView.as_view(), name='partner_profile'),
    path('partnership/', view=views.PartnerProfileView.as_view(), name='partnership' ),
    path('donor-mapping/', view=views.DonorMappingView.as_view(), name='donor_mapping'),
    path('donor-interventions/', view=views.DonorInterventionsView.as_view(), name='donor_interventions'),
    path('donor-programme-results/', view=views.DonorProgrammeResultsView.as_view(), name='donor_programme_results'),
    path('donor-funding/', view=views.DonorFundingView.as_view(), name='donor_funding'),
    path('load-donor-locations/', views.load_donor_locations, name='load_donor_locations'),
    # path('partner-profile-map/', view=views.PartnerProfileMapView.as_view(), name='partner_profile_map'),
    path('interventions/', view=views.InterventionsView.as_view(), name='interventions'),
    path('programmatic-visits-monitoring/', view=views.TripsMonitoringView.as_view(), name='programmatic_visits_monitoring'),
    # path('HACT/', view=views.HACTView.as_view(), name='hact' ),
    path('interventions-export/', view=views.InterventionExportViewSet.as_view(), name='interventions_export'),
]
