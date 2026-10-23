"""The menu each role sees, as a superuser ticked it in the administration's Menu page (web.MenuChoice), kept
in memory like the AI settings (:mod:`neurodb.assistant.llm`): this process's memo (10 s), else the shared
cache, else the table. A save shows at once on the worker that saved it and within 10 seconds on every
other; a warm page makes no query for it.

No row for a role: the focused menu (:data:`neurodb.indicators.services.navigation.FOCUSED`)."""

from __future__ import annotations

import logging
import time

from django.core.cache import caches
from django.db import DatabaseError, transaction
from django.db.models.signals import post_delete, post_save

logger = logging.getLogger(__name__)

MEMO_SECONDS = 10.0  # how long a process keeps the choices it read
SHARED_SECONDS = 300  # how long the shared cache keeps them (a save writes them again once committed)
CACHE_KEY = "neurodb.menu-choices.v1"

_memo: tuple[float, dict[str, list[str]]] | None = None
_pinned = False  # the tests' memo never expires (tests/conftest.py)


def _read() -> dict[str, list[str]]:
    """Every role's row, in one query: ``{role: [entry, ...]}``."""
    from neurodb.web.models import MenuChoice

    return {role: list(shown or []) for role, shown in MenuChoice.objects.values_list("role", "shown")}


def _kept(value) -> dict[str, list[str]] | None:
    """The choices the shared cache held, or None when it held nothing usable."""
    if not isinstance(value, dict):
        return None
    if not all(isinstance(role, str) and isinstance(shown, list) for role, shown in value.items()):
        return None
    return value


def choices() -> dict[str, list[str]]:
    """Every role's saved choice as this process knows it: its memo (10 s), else the shared cache, else the
    table. Choices read from the table are added to the shared cache only when none are there (``add``): the
    ones a save wrote once committed (:func:`_saved`) are never replaced by a read made before that commit.
    When the table cannot be read (a release's first page before its migration ran) every role gets the
    focused menu for now, and nothing is kept."""
    global _memo
    now = time.monotonic()
    if _memo is not None and (_pinned or now - _memo[0] < MEMO_SECONDS):
        return _memo[1]
    shared = caches["shared"]
    try:
        found = _kept(shared.get(CACHE_KEY))
    except Exception:  # the shared cache unreachable: the table still answers
        logger.warning("Menu choices: the shared cache could not be read", exc_info=True)
        found = None
    if found is None:
        try:
            with transaction.atomic():  # a savepoint: a failed read leaves the caller's transaction usable
                found = _read()
        except DatabaseError:
            logger.warning("Menu choices could not be read: the focused menu applies for now", exc_info=True)
            return {}
        try:
            shared.add(CACHE_KEY, found, SHARED_SECONDS)
        except Exception:
            logger.warning("Menu choices: the shared cache could not be written", exc_info=True)
    _memo = (now, found)
    return found


def for_role(role) -> frozenset[str]:
    """The entries ``role``'s menu shows: its saved choice, less any name the menu no longer has, or the
    focused menu while none is saved."""
    from neurodb.indicators.services import navigation

    saved = choices().get(role)
    if saved is None:
        return navigation.FOCUSED
    return frozenset(saved) & frozenset(navigation.keys())


def forget() -> None:
    """Drop the choices (this process's memo and the shared cache's copy): the next read takes the table."""
    global _memo
    _memo = None
    try:
        caches["shared"].delete(CACHE_KEY)
    except Exception:
        logger.warning("Menu choices: the shared cache could not be cleared", exc_info=True)


def _share_committed() -> None:
    """Once a save is committed: this process reads the table again, and the shared cache holds the choices
    as committed, whatever another worker put there in between. When the table cannot be read, or the key
    still holds something else afterwards, the shared cache's copy is dropped instead."""
    global _memo
    _memo = None
    try:
        with transaction.atomic():
            found = _read()
    except DatabaseError:
        logger.warning("Menu choices could not be read after a save", exc_info=True)
        forget()
        return
    shared = caches["shared"]
    try:
        shared.set(CACHE_KEY, found, SHARED_SECONDS)  # the database cache gives up quietly on a held key
        held = shared.get(CACHE_KEY)
    except Exception:
        logger.warning("Menu choices: the shared cache could not be written", exc_info=True)
        held = None
    if held != found:
        forget()


def _saved(sender, **kwargs) -> None:
    forget()
    # and once the save is committed, the choices as committed are shared: another worker may have read the
    # table as it was in between, and adds what it read only to an empty key
    transaction.on_commit(_share_committed, robust=True)  # a failure there never fails the save


post_save.connect(_saved, sender="web.MenuChoice", dispatch_uid="neurodb.web.menu_choices.saved")
post_delete.connect(_saved, sender="web.MenuChoice", dispatch_uid="neurodb.web.menu_choices.deleted")
