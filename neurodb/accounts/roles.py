"""Role model: three groups created by a data migration and checked by mixins/permissions."""

from django.contrib.auth.models import Group, Permission

VIEWER = "Viewer"
SECTION_EDITOR = "Section editor"
ADMIN = "Administrator"
ALL_ROLES = (VIEWER, SECTION_EDITOR, ADMIN)
# Not a role: a group the administrators fill (e.g. Representative, Deputy, section chiefs) whose members
# also get the whole-country view of NeuroDB Watch on For you. A member still needs one of the roles.
MANAGEMENT = "Management"


def role_of(user):
    """The person's role, read once per request (the context processor and the page both ask; outside a
    request it is read each time)."""
    if not user.is_authenticated:
        return None
    if user.is_superuser:
        return ADMIN
    from neurodb.core import compute

    return compute.per_request(f"role:{user.pk}", lambda: _role_held(user))


def _role_held(user):
    held = set(
        user.groups.filter(name__in=(ADMIN, SECTION_EDITOR)).values_list("name", flat=True)
    )  # one query
    if ADMIN in held:
        return ADMIN
    if SECTION_EDITOR in held:
        return SECTION_EDITOR
    return VIEWER


def ensure_groups():
    """Create the three role groups and give Administrators every model permission; create the
    Management group too, with no permissions.

    The admin lists a model only for users holding its permissions, so without this an
    Administrator who is not a superuser would not see a table added by a deployment. Runs at
    every start (``bootstrap_roles``), so the grant follows the models. Returns the role groups only:
    setting a role removes the other returned groups, which must never take a person out of
    Management.
    """
    groups = {name: Group.objects.get_or_create(name=name)[0] for name in ALL_ROLES}
    groups[ADMIN].permissions.set(Permission.objects.all())
    Group.objects.get_or_create(name=MANAGEMENT)
    return groups


def in_management(user) -> bool:
    """A member of the Management group (the whole-country view of NeuroDB Watch, with Administrators)."""
    return bool(user.is_authenticated) and user.groups.filter(name=MANAGEMENT).exists()


def can_set_up(user) -> bool:
    """Whether the person sees the muted "Set up" lines and menu rows of a source that is not live yet: the
    people who can act on them in the admin, a superuser or an Administrator with staff status (the admin
    refuses an Administrator without it, and a Viewer with it has nothing to set up)."""
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_superuser or (user.is_staff and role_of(user) == ADMIN))


def can_edit_section(user, section_id):
    role = role_of(user)
    if role == ADMIN:
        return True
    return role == SECTION_EDITOR and getattr(user, "section_id", None) == section_id
