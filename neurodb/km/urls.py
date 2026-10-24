from django.urls import path, register_converter

from . import views


class KMID:
    """A Finding ID or Document ID: letters, digits, ".", "_" and "-", at most 40."""

    regex = r"[A-Za-z0-9._-]{1,40}"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


register_converter(KMID, "kmid")

app_name = "km"

urlpatterns = [
    path("", views.index, name="index"),
    path("documents/", views.documents, name="documents"),
    path("findings/<kmid:finding_id>/", views.finding, name="finding"),
    path("documents/<kmid:record_id>/", views.document, name="document"),
]
