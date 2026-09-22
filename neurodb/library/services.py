"""Library (resources) and maps services."""

from __future__ import annotations

from django.core.paginator import Paginator
from django.db.models import Q

from .models import Map, Resource, ResourceTag, ResourceTopic, ResourceType


def resource_filters():
    years = (
        Resource.objects.filter(published=True)
        .exclude(publication_year__isnull=True)
        .values_list("publication_year", flat=True)
        .distinct()
        .order_by("-publication_year")
    )
    return {
        "years": list(years),
        "types": ResourceType.objects.order_by("name"),
        "topics": ResourceTopic.objects.order_by("name"),
        "tags": ResourceTag.objects.order_by("name"),
        "sections": Resource.objects.filter(published=True)
        .exclude(section__isnull=True)
        .exclude(section="")
        .values_list("section", flat=True)
        .distinct()
        .order_by("section"),
    }


def search_resources(params, page=1, per_page=12):
    qs = (
        Resource.objects.filter(published=True)
        .select_related("type", "topic")
        .prefetch_related("tags")
        .order_by("-publication_year", "title")
    )
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    for key, field in (
        ("year", "publication_year"),
        ("type", "type_id"),
        ("topic", "topic_id"),
        ("section", "section"),
    ):
        values = (
            [v for v in params.getlist(key) if v]
            if hasattr(params, "getlist")
            else [params.get(key)]
            if params.get(key)
            else []
        )
        if values:
            qs = qs.filter(**{f"{field}__in": values})
    tags = [t for t in params.getlist("tag") if t] if hasattr(params, "getlist") else []
    if tags:
        qs = qs.filter(tags__id__in=tags).distinct()
    return Paginator(qs, per_page).get_page(page)


def completed_maps():
    return Map.objects.filter(status="Completed").order_by("-modified")
