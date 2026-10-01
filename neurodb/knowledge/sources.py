"""Library publications and country programme documents, read into the knowledge base like any added
document, so their full text is searched, quoted and linked.

``sync()`` (run every night before the knowledge hub, and when one is saved) adds the new ones,
reads again the changed ones (file name, file size and summary tell) and removes those no longer published.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db.models import F, Func, IntegerField, TextField, Value
from django.db.models.functions import MD5, Coalesce

from .models import Document

logger = logging.getLogger(__name__)


class OctetLength(Func):
    function = "octet_length"
    output_field = IntegerField()


def _library() -> dict[int, dict[str, Any]]:
    from neurodb.library.models import Resource

    out = {}
    for r in (
        Resource.objects.filter(published=True)
        .annotate(
            size=OctetLength(F("resource_file")),
            summary=MD5(Coalesce("description", Value(""), output_field=TextField())),
        )
        .values("pk", "title", "publication_year", "section", "resource_file_name", "size", "summary")
    ):
        year = str(r["publication_year"] or "")[:4]
        out[r["pk"]] = {
            "title": r["title"] or f"Publication {r['pk']}",
            "source": "Library publication",
            "section": r["section"],
            "year": int(year) if year.isdigit() else None,
            "signature": f"{r['resource_file_name'] or ''}:{r['size'] or 0}:{r['summary']}"[:300],
            "has_file": bool(r["size"]),
        }
    return out


def _cpd() -> dict[int, dict[str, Any]]:
    from neurodb.cpd.models import CPDocument

    out = {}
    for d in CPDocument.objects.select_related("programme"):
        try:
            size = d.file.size
        except (OSError, ValueError):
            size = 0
        out[d.pk] = {
            "title": d.title,
            "source": f"{d.programme.name} — {d.get_kind_display()}",
            "section": None,
            "year": d.programme.start_year,
            "signature": f"{d.file.name}:{size}"[:300],
            "has_file": bool(size),
        }
    return out


def source_bytes(document: Document) -> tuple[str, bytes] | None:
    """The file of a library publication or CPD document (None when it has none)."""
    if document.origin == Document.Origin.LIBRARY:
        from neurodb.library.models import Resource

        r = Resource.objects.filter(pk=document.origin_id).only("resource_file", "resource_file_name").first()
        if r and r.resource_file:
            return r.resource_file_name or "publication.pdf", bytes(r.resource_file)
        return None
    if document.origin == Document.Origin.CPD:
        from neurodb.cpd.models import CPDocument

        d = CPDocument.objects.filter(pk=document.origin_id).first()
        if d and d.file:
            with d.file.open("rb") as handle:
                return d.filename, handle.read()
    return None


def source_text(document: Document) -> str:
    """The text of a publication without a file: its title and summary."""
    if document.origin == Document.Origin.LIBRARY:
        from neurodb.library.models import Resource

        r = Resource.objects.filter(pk=document.origin_id).only("title", "description").first()
        return f"{r.title}\n\n{r.description or ''}" if r else ""
    return ""


def sync(origin: str | None = None, origin_id: int | None = None) -> dict[str, list[int]]:
    """Add, refresh and remove the documents of the library and the CPD; returns the ids to read."""
    from neurodb.accounts.models import Section

    sections = {s.name.lower(): s.pk for s in Section.objects.all()}
    to_read, removed = [], []
    for kind, wanted in ((Document.Origin.LIBRARY, _library), (Document.Origin.CPD, _cpd)):
        if origin and origin != kind:
            continue
        current = wanted()
        if origin_id is not None:
            current = {k: v for k, v in current.items() if k == origin_id}
        existing = {d.origin_id: d for d in Document.objects.filter(origin=kind)}
        if origin_id is not None:
            existing = {k: v for k, v in existing.items() if k == origin_id}
        for pk, info in current.items():
            doc = existing.pop(pk, None)
            if (
                doc is not None
                and doc.origin_signature == info["signature"]
                and doc.status != Document.Status.FAILED
            ):
                continue
            doc = doc or Document(origin=kind, origin_id=pk)
            doc.title = info["title"][:300]
            doc.source = info["source"][:300]
            doc.year = info["year"]
            doc.section_id = sections.get((info["section"] or "").lower())
            doc.origin_signature = info["signature"]
            doc.status = Document.Status.PENDING
            doc.save()
            to_read.append(doc.pk)
        for doc in existing.values():  # no longer published or deleted
            removed.append(doc.pk)
            doc.delete()
    return {"read": to_read, "removed": removed}
