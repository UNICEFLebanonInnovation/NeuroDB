"""Who may change what in Monitoring insights: its settings, overrides and versions are for
Administrators only (superusers included); everyone else reads."""

from __future__ import annotations

from neurodb.accounts.roles import ADMIN, role_of


def is_admin(user) -> bool:
    """An Administrator (the role, or a superuser)."""
    return user is not None and role_of(user) == ADMIN


def can_review(user, visit) -> bool:
    """May mark ``visit`` reviewed: an Administrator, or a Section editor of one of the visit's
    sections (``Visit.section_ids``, the NeuroDB sections its eTools sections are confirmed to be)."""
    from neurodb.accounts.roles import can_edit_section

    if user is None or not user.is_authenticated:
        return False
    return is_admin(user) or any(can_edit_section(user, sid) for sid in visit.section_ids or ())
