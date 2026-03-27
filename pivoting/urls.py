from django.urls import path
from pivoting.views import *

urlpatterns = [
    path('load_database_activityinfo/', load_database_activityinfo, name='load_database_activityinfo'),
    path('load_neuroreport_activityinfo/', load_neuroreport_activityinfo, name='load_neuroreport_activityinfo'),
    path('load_pca_activityinfo/', load_pca_activityinfo, name='load_pca_activityinfo'),
    path('load_sub_indicators/', load_sub_indicators, name='load_sub_indicators'),
    path('load_donor_activityinfo/', load_donor_activityinfo, name='load_donor_activityinfo'),
    path('load_partner_staff/', load_partner_staff, name='load_partner_staff'),
    path('load_pca_details/', load_pca_details, name='load_pca_details'),
    path('load_donors_mapping_data/', load_donors_mapping_data, name='load_donors_mapping_data'),
    path('load_donors_mapping_locations/', load_donors_mapping_locations, name='load_donors_mapping_locations'),
    path('load_database_intervention_locations/', load_database_intervention_locations, name='load_database_intervention_locations'),
    path('load_database_snapshot/', load_database_snapshot, name='load_database_snapshot'),
    path('load_ry_master_indicators/', load_ry_master_indicators, name='load_ry_master_indicators'),
    path('load_population_figures/', load_population_figures, name='load_population_figures'),
    path('load_most_vulnerable/', load_most_vulnerable, name='load_most_vulnerable'),

    path('database-dashboard', view=DatabaseDashboardView.as_view(), name='database-dashboard'),
    path('database-analytical', view=DatabaseAnalyticalView.as_view(), name='database-analytical'),
    path('database-snapshot', view=DatabaseSnapshotView.as_view(), name='database-snapshot'),
    path('database-intervention-map/', view=DatabaseInterventionMapView.as_view(), name='database_intervention_map'),
    path('database-download/', view=ExportViewSet.as_view(), name='database-download'),

    path('neuroreport-dashboard/', view=NeuroReportDashboardView.as_view(), name='neuroreport-dashboard'),
    path('neuroreport-analytical', view=NeuroReportAnalyticalView.as_view(), name='neuroreport-analytical'),
    path('hpm-neuroreport/', view=HPMNeuroReportView.as_view(), name='hpm-neuroreport'),

    path('pca-dashboard/', view=PCADashboardView.as_view(), name='pca-dashboard'),
    path('pca-analytical', view=PCAAnalyticalView.as_view(), name='pca-analytical'),
    path('pca-summary-active', view=PCASummaryActiveView.as_view(), name='pca-summary-active'),
    path('pca-summary-all', view=PCASummaryAllView.as_view(), name='pca-summary-all'),

    path('donor-dashboard/', view=DonorDashboardView.as_view(), name='donor-dashboard'),

    path('partnerships/', view=PartnershipsView.as_view(), name='partnerships'),
    path('partnership-profile/', view=PartnershipProfileView.as_view(), name='partnership-profile'),
    path('population-figures/', view=PopulationFiguresView.as_view(), name='population-figures'),
    path('Resources/', ResourcesView.as_view(), name='resources'),
    path('resource_file/<int:pk>/', resource_file, name='resource_file'),
    path('activityinfo-summary-download/', ActivityInfoSummaryDownload, name='activityinfo-summary-download'),
    path('activityinfo-pca-summary-download/', ActivityInfoPCASummaryDownload, name='activityinfo-pca-summary-download'),   
    path('etools-summary-excel/', EtoolsSummaryExcel, name='etools-summary-excel'),
    path('activityinfo-wrong-pca-numbers/', get_wrong_pds, name='activityinfo-wrong-pca-numbers'),
    path('Maps/', view=MapsView.as_view(), name='maps'),

]
