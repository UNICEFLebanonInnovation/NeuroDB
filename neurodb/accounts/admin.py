from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db.models import Count, Q
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from neurodb.web.admin_helpers import badge

from .models import Office, Section, User
from .roles import ADMIN, ALL_ROLES, SECTION_EDITOR, VIEWER, ensure_groups

ROLE_TONES = {ADMIN: "bad", SECTION_EDITOR: "info", VIEWER: "ok"}


class RoleFilter(admin.SimpleListFilter):
    """Filter by NeuroDB role; 'none' lists users who fall back to viewer because no role was given."""

    title = _("role")
    parameter_name = "role"

    def lookups(self, request, model_admin):
        return (
            ("viewer", VIEWER),
            ("editor", SECTION_EDITOR),
            ("admin", _("Administrator (incl. superusers)")),
            ("none", _("No role assigned")),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "viewer":
            return queryset.filter(groups__name=VIEWER).distinct()
        if value == "editor":
            return queryset.filter(groups__name=SECTION_EDITOR).distinct()
        if value == "admin":
            return queryset.filter(Q(is_superuser=True) | Q(groups__name=ADMIN)).distinct()
        if value == "none":
            return queryset.filter(is_superuser=False).exclude(groups__name__in=ALL_ROLES)
        return queryset


@admin.register(User)
class UserAdmin(DjangoUserAdmin, ModelAdmin):
    # Unfold's versions of the user forms, so the password fields match the theme.
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    list_display = ("username", "email", "full_name", "section", "role", "is_active", "last_login")
    list_filter = (RoleFilter, "is_active", "section", "is_staff", "is_superuser")
    list_select_related = ("section",)
    search_fields = ("username", "email", "first_name", "last_name", "section__name")
    ordering = ("username",)
    actions = ("make_viewer", "make_section_editor", "make_administrator", "activate", "deactivate")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (
            _("Person"),
            {"fields": ("first_name", "last_name", "email", "section", "backup_user", "skype_account")},
        ),
        (
            _("Access"),
            {
                "fields": ("is_active", "groups", "is_staff", "is_superuser"),
                "description": _(
                    "The role is the group: Viewer, Section editor or Administrator. "
                    "Staff can open this admin; superusers bypass every permission."
                ),
            },
        ),
        (_("Fine-grained permissions"), {"classes": ("collapse",), "fields": ("user_permissions",)}),
        (_("Dates"), {"fields": ("last_login", "date_joined")}),
    )
    readonly_fields = ("last_login", "date_joined")
    autocomplete_fields = ("backup_user",)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("groups")

    @admin.display(description=_("Name"), ordering="last_name")
    def full_name(self, obj):
        return obj.get_full_name() or "—"

    @admin.display(description=_("Role"))
    def role(self, obj):
        if obj.is_superuser:
            return badge(_("Superuser"), "bad")
        names = {g.name for g in obj.groups.all()}
        for name in (ADMIN, SECTION_EDITOR, VIEWER):
            if name in names:
                return badge(name, ROLE_TONES[name])
        return badge(_("None (viewer)"), "muted")

    def _set_role(self, request, queryset, role):
        """Roles are exclusive: drop the other two role groups, keep any unrelated groups."""
        groups = ensure_groups()
        others = [g for name, g in groups.items() if name != role]
        count = 0
        for user in queryset:
            user.groups.remove(*others)
            user.groups.add(groups[role])
            count += 1
        self.message_user(
            request, _("%(n)s user(s) are now %(role)s.") % {"n": count, "role": role}, messages.SUCCESS
        )

    @admin.action(description=_("Set role: Viewer"), permissions=("change",))
    def make_viewer(self, request, queryset):
        self._set_role(request, queryset, VIEWER)

    @admin.action(description=_("Set role: Section editor"), permissions=("change",))
    def make_section_editor(self, request, queryset):
        missing = queryset.filter(section__isnull=True).count()
        self._set_role(request, queryset, SECTION_EDITOR)
        if missing:
            self.message_user(
                request,
                _("%(n)s of them have no section, so they cannot edit anything yet.") % {"n": missing},
                messages.WARNING,
            )

    @admin.action(description=_("Set role: Administrator"), permissions=("change",))
    def make_administrator(self, request, queryset):
        self._set_role(request, queryset, ADMIN)

    @admin.action(description=_("Activate selected users"), permissions=("change",))
    def activate(self, request, queryset):
        n = queryset.update(is_active=True)
        self.message_user(request, _("%(n)s user(s) activated.") % {"n": n}, messages.SUCCESS)

    @admin.action(description=_("Deactivate selected users"), permissions=("change",))
    def deactivate(self, request, queryset):
        n = queryset.exclude(pk=request.user.pk).update(is_active=False)
        self.message_user(
            request, _("%(n)s user(s) deactivated (never yourself).") % {"n": n}, messages.SUCCESS
        )


@admin.register(Section)
class SectionAdmin(ModelAdmin):
    list_display = ("name", "code", "user_count", "have_hpm_indicator", "powerbi_url")
    search_fields = ("name", "code")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(users_n=Count("user", distinct=True))

    @admin.display(description=_("Users"), ordering="users_n")
    def user_count(self, obj):
        return obj.users_n


@admin.register(Office)
class OfficeAdmin(ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
