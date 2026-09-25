"""Role model: three groups created by a data migration and checked by mixins/permissions."""

from django.contrib.auth.models import Group, Permission

VIEWER = "Viewer"
SECTION_EDITOR = "Section editor"
ADMIN = "Administrator"
ALL_ROLES = (VIEWER, SECTION_EDITOR, ADMIN)


def role_of(user):
    if not user.is_authenticated:
        return None
    if user.is_superuser or user.groups.filter(name=ADMIN).exists():
        return ADMIN
    if user.groups.filter(name=SECTION_EDITOR).exists():
        return SECTION_EDITOR
    return VIEWER


def ensure_groups():
    """Create the three role groups and give Administrators every model permission.

    The admin lists a model only for users holding its permissions, so without this an
    Administrator who is not a superuser would not see a table added by a deployment. Runs at
    every start (``bootstrap_roles``), so the grant follows the models.
    """
    groups = {name: Group.objects.get_or_create(name=name)[0] for name in ALL_ROLES}
    groups[ADMIN].permissions.set(Permission.objects.all())
    return groups


def can_edit_section(user, section_id):
    role = role_of(user)
    if role == ADMIN:
        return True
    return role == SECTION_EDITOR and getattr(user, "section_id", None) == section_id
