"""Internal JSON API behind the pages (session-authenticated, same services as the HTML views)."""

from __future__ import annotations

from typing import Any

from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.gzip import gzip_page
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from neurodb.core.models import SavedView
from neurodb.facts.services import dashboard as facts
from neurodb.indicators.models import Database, MasterIndicator, NeuroReport
from neurodb.partnerships import services as partnerships

from . import services
from .forms import SavedViewForm


def _bad_request(detail: str, **extra: Any) -> Response:
    return Response({"detail": detail, **extra}, status=status.HTTP_400_BAD_REQUEST)


def _database(pk: int) -> Database:
    return get_object_or_404(Database.objects.select_related("section", "reporting_year"), pk=pk)


def _report(pk: int) -> NeuroReport:
    return get_object_or_404(NeuroReport.objects.select_related("ryear"), pk=pk, is_active=True)


class DashboardAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        return Response(facts.database_dashboard(_database(pk)).as_dict())


@method_decorator(gzip_page, name="dispatch")
class AnalyticalAPI(APIView):
    """Flat pivot rows. Compact JSON list; gzip makes the typical 5-50k rows a few hundred kB."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        emergency = request.query_params.get("emergency", "")
        if emergency and emergency not in services.EMERGENCY_VALUES:
            return _bad_request("emergency must be 'yes' or 'no'.", allowed=list(services.EMERGENCY_VALUES))
        return Response(facts.analytical_rows(_database(pk), emergency=emergency or None))


@method_decorator(gzip_page, name="dispatch")
class ReportAnalyticalAPI(APIView):
    """Pivot rows of every database behind a Neuro Report, limited to the report's master indicators."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        report = _report(pk)
        emergency = request.query_params.get("emergency", "")
        if emergency and emergency not in services.EMERGENCY_VALUES:
            return _bad_request("emergency must be 'yes' or 'no'.", allowed=list(services.EMERGENCY_VALUES))
        wanted = {
            f"{m.awp_code}_{m.name}"
            for m in MasterIndicator.objects.filter(neuroreportmasterindicator__report=report).only("awp_code", "name")
        }
        rows: list[dict[str, Any]] = []
        for database in services.report_databases(report):
            rows.extend(r for r in facts.analytical_rows(database, emergency=emergency or None) if r["master_indicator"] in wanted)
        return Response(rows)


class MapAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        level = request.query_params.get("level", "governorate")
        if level not in services.MAP_LEVELS:
            return _bad_request("Unknown level.", allowed=list(services.MAP_LEVELS))
        return Response(facts.map_data(_database(pk), level=level, **services.map_filters(request.query_params)))


class IndicatorDetailAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int, master_id: int) -> Response:
        database = _database(pk)
        master = get_object_or_404(MasterIndicator, pk=master_id, database=database)
        return Response({"master": {"id": master.id, "name": master.name, "awp_code": master.awp_code}, "rows": facts.master_detail(database, master.id)})


class HPMAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request, pk: int) -> Response:
        report = _report(pk)
        raw_month = request.query_params.get("month", "")
        month = services.parse_month(raw_month)
        if raw_month and month is None:
            return _bad_request("month must be an integer from 1 to 12.")
        raw_quarter = request.query_params.get("quarter", "")
        quarter = services.parse_quarter(raw_quarter)
        if raw_quarter and quarter is None:
            return _bad_request("quarter must be one of Q1..Q4.", allowed=list(services.QUARTERS))
        data = facts.neuroreport(report, month=month, quarter=quarter)
        return Response(
            {
                "report": {"id": report.id, "name": report.name, "is_hpm": report.is_hpm},
                "year": data["year"],
                "month": data["month"],
                "month_label": data["month_label"],
                "quarter": data["quarter"],
                "cutoff": data["cutoff"].isoformat(),
                "sections": [
                    {"database": {"id": s["database"].id, "name": s["database"].label or s["database"].name}, "items": s["items"]}
                    for s in data["sections"]
                ],
                "comments": [
                    {"id": c.id, "master_id": c.master_id, "comment": c.comment, "month": c.related_month, "entry_date": c.entry_date.isoformat()}
                    for c in data["comments"]
                ],
                "totals": data["totals"],
            }
        )


class ProgrammesAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        scope = request.query_params.get("scope", "all")
        if scope not in services.PD_SCOPES:
            return _bad_request("scope must be 'active' or 'all'.")
        filters = partnerships.PDFilters.from_params(request.query_params)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(partnerships.programme_documents(filters, scope=scope), request, view=self)
        counts = partnerships.pd_intervention_counts([pd.number for pd in page if pd.number])
        return paginator.get_paginated_response([services.pd_as_dict(pd, counts) for pd in page])


class DonorsAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        data = partnerships.donor_mapping(partnerships.PDFilters.from_params(request.query_params))
        data["programmes"] = [{**services.pd_as_dict(p["pd"]), "donations": p["donations"]} for p in data["programmes"]]
        return Response(data)


class SavedViewsAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        page = request.query_params.get("page", "")
        raw_object = request.query_params.get("object_id", "")
        object_id = int(raw_object) if raw_object.isdigit() else None
        if not page:
            return _bad_request("page is required.")
        views = services.saved_views_for(request.user, page, object_id)
        return Response([services.saved_view_as_dict(v, request.user) for v in views])

    def post(self, request: Request) -> Response:
        form = SavedViewForm(request.data)
        if not form.is_valid():
            return _bad_request("Invalid saved view.", errors=form.errors.get_json_data())
        data = form.cleaned_data
        view, created = SavedView.objects.update_or_create(
            owner=request.user,
            page=data["page"],
            object_id=data["object_id"],
            name=data["name"],
            defaults={"query": data["query"], "layout": data["layout"], "is_shared": data["is_shared"]},
        )
        return Response(services.saved_view_as_dict(view, request.user), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class SavedViewDetailAPI(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request: Request, pk: int) -> Response:
        view = get_object_or_404(SavedView, pk=pk)
        if not services.can_delete_saved_view(request.user, view):
            return Response({"detail": "Not your view."}, status=status.HTTP_403_FORBIDDEN)
        view.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class HealthAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(services.health_as_dict(services.data_health()))
