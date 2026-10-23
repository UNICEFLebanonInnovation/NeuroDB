"""The administration's Menu page (Users and access → Menu, superusers only): one tick per menu entry and
role, saying which entries each role's sidebar shows. Superusers always see the whole menu; a role with no
saved choice sees the focused menu (:data:`neurodb.indicators.services.navigation.FOCUSED`). Only the menu
changes: a page left out of it still opens from links, the search and bookmarks."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, VIEWER
from neurodb.indicators.services import navigation

from .models import MenuChoice

ROLES = (ADMIN, SECTION_EDITOR, VIEWER)  # the page's columns, in this order
NAMES = {ADMIN: _("Administrator"), SECTION_EDITOR: _("Section editor"), VIEWER: _("Viewer")}
PLURALS = {ADMIN: _("Administrators"), SECTION_EDITOR: _("Section editors"), VIEWER: _("Viewers")}


def _field(role: str) -> str:
    """A role's field in the form (``role-viewer``): one checkbox per entry carries it."""
    return "role-" + slugify(role)


def _superuser(request) -> bool:
    return bool(request.user.is_active and request.user.is_superuser)


class MenuForm(forms.Form):
    """The ticks of every role: one list of entries per role, each a name of :func:`navigation.keys`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(key, key) for key in navigation.keys()]
        for role in ROLES:
            self.fields[_field(role)] = forms.MultipleChoiceField(choices=choices, required=False)

    def clean(self):
        data = super().clean()
        for role in ROLES:
            name = _field(role)
            # an empty sidebar is a slip, not a choice (the logo and the search would still work)
            if name in data and not data[name]:
                self.add_error(name, _("Tick at least one entry for %(role)s.") % {"role": PLURALS[role]})
        return data


@admin.register(MenuChoice)
class MenuChoiceAdmin(ModelAdmin):
    """The Menu page is this model's list (:meth:`changelist_view`): a grid of ticks, Save and Reset. A row
    saved for a role opens read only, with its history. Only a superuser opens any of it: Administrators hold
    every permission (``ensure_groups``), so the permissions are checked here, not left to the groups."""

    fields = ("role", "entries_shown", "updated_by", "updated_at")
    readonly_fields = ("entries_shown",)

    def has_module_permission(self, request):
        return _superuser(request)

    def has_view_permission(self, request, obj=None):
        return _superuser(request)

    def has_change_permission(self, request, obj=None):
        # the page itself only: a row's own form would let the entries be typed in by hand, outside the grid
        return obj is None and _superuser(request)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Entries shown"))
    def entries_shown(self, obj):
        labels = {entry["key"]: str(entry["label"]) for entry in navigation.entries()}
        shown = set(obj.shown)
        return ", ".join(labels[key] for key in navigation.keys() if key in shown) or "—"

    def changelist_view(self, request, extra_context=None):
        # the admin's own check (view or change) is not made for a list drawn here: it is made first
        if not self.has_view_permission(request):
            raise PermissionDenied
        form = None
        if request.method == "POST":
            if not self.has_change_permission(request):
                raise PermissionDenied
            if "reset" in request.POST:
                return self._reset(request)
            form = MenuForm(request.POST)
            if form.is_valid():
                return self._save(request, form)
        return render(request, "admin/web/menuchoice/menu.html", self._context(request, form))

    def _context(self, request, form: MenuForm | None) -> dict:
        keys = navigation.keys()
        rows = {row.role: row for row in MenuChoice.objects.all()}
        if form is not None:  # what was ticked, drawn again with its errors
            ticked = {role: set(form.data.getlist(_field(role))) for role in ROLES}
        else:
            ticked = {
                role: set(rows[role].shown) & set(keys) if role in rows else set(navigation.FOCUSED)
                for role in ROLES
            }
        sections: list[dict] = []
        for n, entry in enumerate(navigation.entries()):
            if not sections or str(sections[-1]["title"]) != str(entry["group"]):
                sections.append({"title": entry["group"], "rows": []})
            cells = [
                {
                    "name": _field(role),
                    "id": f"m-{slugify(role)}-{n}",
                    "ticked": entry["key"] in ticked[role],
                    "label": _("Show %(entry)s to %(role)s")
                    % {"entry": entry["label"], "role": PLURALS[role]},
                }
                for role in ROLES
            ]
            sections[-1]["rows"].append({**entry, "cells": cells})
        roles = [
            {
                "name": NAMES[role],
                "count": len(ticked[role] & set(keys)),
                "history": (
                    reverse("admin:web_menuchoice_history", args=[rows[role].pk]) if role in rows else ""
                ),
            }
            for role in ROLES
        ]
        errors = [error for field in (form.errors.values() if form else []) for error in field]
        return {
            **self.admin_site.each_context(request),
            "title": _("Menu"),
            "opts": self.model._meta,
            "sections": sections,
            "roles": roles,
            "columns": len(ROLES) + 1,
            "errors": errors,
        }

    def _save(self, request, form: MenuForm):
        """Each role's ticks: exactly the focused menu removes the role's row; anything else is saved in it.
        Each role that changed is recorded in the audit trail with the entries added and removed."""
        keys = navigation.keys()
        labels = {entry["key"]: str(entry["label"]) for entry in navigation.entries()}
        counts = {}
        with transaction.atomic():
            rows = {row.role: row for row in MenuChoice.objects.select_for_update()}
            for role in ROLES:
                chosen = set(form.cleaned_data[_field(role)])
                ticked = [key for key in keys if key in chosen]
                counts[role] = len(ticked)
                row = rows.get(role)
                if frozenset(ticked) == navigation.FOCUSED:
                    if row is not None:
                        self.log_deletions(request, MenuChoice.objects.filter(pk=row.pk))
                        row.delete()
                    continue
                before = set(row.shown) & set(keys) if row is not None else set(navigation.FOCUSED)
                if row is not None and before == chosen:
                    continue
                row, created = MenuChoice.objects.update_or_create(
                    role=role, defaults={"shown": ticked, "updated_by": request.user}
                )
                message = _changes(before, chosen, keys, labels)
                (self.log_addition if created else self.log_change)(request, row, message)
        messages.success(
            request,
            _("Saved. Entries ticked: Administrator %(admin)s, Section editor %(editor)s, Viewer %(viewer)s.")
            % {"admin": counts[ADMIN], "editor": counts[SECTION_EDITOR], "viewer": counts[VIEWER]},
        )
        return redirect("admin:web_menuchoice_changelist")

    def _reset(self, request):
        with transaction.atomic():
            rows = MenuChoice.objects.all()
            if rows.exists():
                self.log_deletions(request, rows)
                rows.delete()
        messages.success(request, _("Every role sees the focused menu again."))
        return redirect("admin:web_menuchoice_changelist")


def _changes(before: set[str], after: set[str], keys: list[str], labels: dict[str, str]) -> str:
    """The audit trail's words for a role's change: the entries added and removed, by their labels."""
    added = [labels[key] for key in keys if key in after - before]
    removed = [labels[key] for key in keys if key in before - after]
    parts = []
    if added:
        parts.append(str(_("Entries added: %(names)s.") % {"names": ", ".join(added)}))
    if removed:
        parts.append(str(_("Entries removed: %(names)s.") % {"names": ", ".join(removed)}))
    return " ".join(parts)
