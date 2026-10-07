from django.urls import path

from . import review_views, views

app_name = "knowledge"

urlpatterns = [
    path("", views.index, name="index"),
    path("add/", views.add, name="add"),
    path("reports/", views.series_index, name="series_index"),
    path("reports/<int:pk>/", views.series_detail, name="series"),
    # the document review (FMS §9 "Other Reports")
    path("review/", review_views.page, name="review"),
    path("review/desk-review.docx", review_views.desk_review, name="review_docx"),
    path("review/verified/", review_views.verified_only, name="review_verified"),
    path("review/batches/new/", review_views.batch_new, name="review_batch_new"),
    path("review/batches/<int:pk>/", review_views.batch_edit, name="review_batch_edit"),
    path("review/batches/<int:pk>/add/", review_views.batch_add, name="review_batch_add"),
    path("review/documents/<int:pk>/analyse/", review_views.analyse, name="review_analyse"),
    path("review/documents/<int:pk>/reference/", review_views.reference, name="review_reference"),
    path("review/documents/<int:pk>/remove/", review_views.take_out, name="review_remove"),
    path("review/documents/<int:pk>/verdicts/", review_views.bulk_verdict, name="review_bulk_verdict"),
    path("review/documents/<int:pk>/findings/new/", review_views.finding_new, name="review_finding_new"),
    path("review/findings/<int:pk>/verdict/", review_views.finding_verdict, name="review_finding_verdict"),
    path("review/findings/<int:pk>/edit/", review_views.finding_edit, name="review_finding_edit"),
    path("review/findings/<int:pk>/delete/", review_views.finding_delete, name="review_finding_delete"),
    path(
        "review/statements/<int:pk>/verdict/", review_views.statement_verdict, name="review_statement_verdict"
    ),
    path("review/actions/<int:pk>/status/", review_views.action_status, name="review_action_status"),
    path("review/themes/<int:pk>/paragraph/", review_views.theme_paragraph, name="review_paragraph"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/file/", views.download, name="file"),
    path("<int:pk>/reindex/", views.reindex, name="reindex"),
    path("<int:pk>/delete/", views.delete, name="delete"),
]
