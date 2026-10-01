"""A library publication or a CPD document saved or deleted is read into (or removed from) the
knowledge base in the background; the nightly run catches anything missed."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save

logger = logging.getLogger(__name__)


def _start(origin: str, pk: int) -> None:
    from django.conf import settings

    if getattr(settings, "KNOWLEDGE_INDEX_ON_SAVE", True):
        from neurodb.integrations import background

        def start() -> None:
            try:
                background.start_command("index_documents", "--origin", origin, "--id", str(pk))
            except Exception:  # never block the save
                logger.exception("could not start reading %s %s into the knowledge base", origin, pk)

        transaction.on_commit(start)  # the background process must see the saved (or deleted) row


def connect() -> None:
    from neurodb.cpd.models import CPDocument
    from neurodb.library.models import Resource

    post_save.connect(
        lambda sender, instance, **kw: _start("library", instance.pk),
        sender=Resource,
        weak=False,
        dispatch_uid="knowledge_library_saved",
    )
    post_delete.connect(
        lambda sender, instance, **kw: _start("library", instance.pk),
        sender=Resource,
        weak=False,
        dispatch_uid="knowledge_library_deleted",
    )
    post_save.connect(
        lambda sender, instance, **kw: _start("cpd", instance.pk),
        sender=CPDocument,
        weak=False,
        dispatch_uid="knowledge_cpd_saved",
    )
    post_delete.connect(
        lambda sender, instance, **kw: _start("cpd", instance.pk),
        sender=CPDocument,
        weak=False,
        dispatch_uid="knowledge_cpd_deleted",
    )
