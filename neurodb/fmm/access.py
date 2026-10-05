"""Who may change what in Monitoring insights: its settings, overrides and versions are for
Administrators only (superusers included); everyone else reads."""

from __future__ import annotations

from neurodb.accounts.roles import ADMIN, role_of


def is_admin(user) -> bool:
    """An Administrator (the role, or a superuser)."""
    return user is not None and role_of(user) == ADMIN
