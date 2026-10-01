"""Who may add documents to the knowledge base, and who may change or remove one.

Every signed-in user (donor accounts excepted: they only reach the donor page) can read the
knowledge base and ask about it. Administrators and section editors add documents; a document is
changed or removed by an administrator or by the person who added it."""

from __future__ import annotations

from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, role_of


def can_add(user) -> bool:
    return bool(user.is_authenticated and (user.is_superuser or role_of(user) in (ADMIN, SECTION_EDITOR)))


def can_manage(user, document) -> bool:
    """Read again or remove. Publications and CPD documents follow their source: only an
    administrator reads them again, and they are removed with their source."""
    if not user.is_authenticated:
        return False
    if document.origin != "added":
        return user.is_superuser or role_of(user) == ADMIN
    return (
        user.is_superuser
        or role_of(user) == ADMIN
        or (document.added_by_id is not None and document.added_by_id == user.pk and can_add(user))
    )
