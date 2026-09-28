"""Donor accounts in the admin: create the sign-in, choose what it sees, issue a temporary password.

Creating an account creates its user too (never staff, no role), with a temporary password shown
once to the administrator, who passes it to the donor by a separate channel. The donor must replace
it at first sign-in. "Preview" opens the page exactly as the donor sees it.
"""

from __future__ import annotations

import secrets

from django import forms
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.forms import BaseDialogForm

from neurodb.web.admin_helpers import badge

from . import services
from .models import DonorAccount


class ConfirmForm(BaseDialogForm):
    """The confirmation step of the password button."""


def temporary_password() -> str:
    return secrets.token_urlsafe(12)


def _split(text: str) -> list[str]:
    return [part.strip() for part in (text or "").replace("\n", ",").split(",") if part.strip()]


class DonorAccountForm(forms.ModelForm):
    """The donors as a choice of the names eTools writes on funds reservation lines (plus any typed
    in, for a donor with no funds synced yet) and the grants as a comma-separated list."""

    donor_choices = forms.MultipleChoiceField(
        label=_("Donors"),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 10}),
        help_text=_("The donor names on eTools funds reservations. Hold Ctrl to choose several."),
    )
    other_donors = forms.CharField(
        label=_("Other donor names"),
        required=False,
        help_text=_("Comma-separated, exactly as eTools writes them, for a donor not in the list yet."),
    )
    grant_list = forms.CharField(
        label=_("Only these grants"),
        required=False,
        help_text=_("Comma-separated grant numbers. Empty: every grant of the donors above."),
    )

    class Meta:
        model = DonorAccount
        fields = ("name", "show_partner_names", "contact", "active", "expires_on")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        names = services.donor_names()
        current = list(self.instance.donors or []) if self.instance.pk else []
        self.fields["donor_choices"].choices = [(n, n) for n in names]
        self.fields["donor_choices"].initial = [n for n in current if n in names]
        self.fields["other_donors"].initial = ", ".join(n for n in current if n not in names)
        self.fields["grant_list"].initial = ", ".join(self.instance.grants or []) if self.instance.pk else ""

    def clean(self):
        data = super().clean()
        donors = list(data.get("donor_choices") or []) + _split(data.get("other_donors", ""))
        if not donors:
            raise forms.ValidationError(_("Choose at least one donor: the account shows only their funds."))
        data["donors"] = list(dict.fromkeys(donors))
        data["grants"] = list(dict.fromkeys(_split(data.get("grant_list", ""))))
        return data

    def save(self, commit=True):
        self.instance.donors = self.cleaned_data["donors"]
        self.instance.grants = self.cleaned_data["grants"]
        return super().save(commit=commit)


class DonorAccountCreateForm(DonorAccountForm):
    email = forms.EmailField(
        label=_("Donor's email"),
        help_text=_(
            "The sign-in. It must not belong to a NeuroDB user already: staff keep their own account."
        ),
    )

    class Meta(DonorAccountForm.Meta):
        fields = ("email", *DonorAccountForm.Meta.fields)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        User = get_user_model()
        if (
            User.objects.filter(email__iexact=email).exists()
            or User.objects.filter(username__iexact=email).exists()
        ):
            raise forms.ValidationError(_("A NeuroDB user already has this email."))
        return email


@admin.register(DonorAccount)
class DonorAccountAdmin(ModelAdmin):
    list_display = ("name", "sign_in", "donor_list", "state", "expires_on", "last_seen_at", "preview")
    list_filter = ("active",)
    search_fields = ("name", "user__email", "user__username")
    readonly_fields = ("user", "must_change_password", "created_at", "created_by", "last_seen_at", "preview")
    actions = ("issue_password", "switch_off")
    actions_detail = ("issue_password_detail",)

    def get_form(self, request, obj=None, change=False, **kwargs):
        kwargs["form"] = DonorAccountForm if obj else DonorAccountCreateForm
        kwargs["fields"] = None
        return super().get_form(request, obj, change=change, **kwargs)

    def get_fieldsets(self, request, obj=None):
        what = (
            _("What the donor sees"),
            {
                "fields": (
                    "name",
                    "donor_choices",
                    "other_donors",
                    "grant_list",
                    "show_partner_names",
                    "contact",
                )
            },
        )
        if obj is None:
            return ((_("Sign-in"), {"fields": ("email", "expires_on")}), what)
        return (
            (
                _("Sign-in"),
                {"fields": ("user", "active", "expires_on", "must_change_password", "last_seen_at")},
            ),
            what,
            (_("Record"), {"fields": ("created_at", "created_by", "preview")}),
        )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user")

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        if not change:
            password = temporary_password()
            email = form.cleaned_data["email"]
            obj.user = get_user_model().objects.create_user(
                username=email, email=email, password=password, first_name=obj.name[:150]
            )
            obj.created_by = request.user.get_username()
            obj.must_change_password = True
            super().save_model(request, obj, form, change)
            self._show_password(request, obj, password)
            return
        super().save_model(request, obj, form, change)
        if obj.user.is_active != obj.active:
            obj.user.is_active = obj.active
            obj.user.save(update_fields=["is_active"])

    def delete_model(self, request, obj):
        user = obj.user
        super().delete_model(request, obj)
        user.delete()  # the sign-in exists only for this account

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)

    @staticmethod
    def _show_password(request, obj, password: str) -> None:
        messages.warning(
            request,
            format_html(
                "Temporary password for <b>{}</b>: <code style='user-select:all'>{}</code> — shown once. "
                "Send it to the donor separately from the sign-in address; they must change it at first "
                "sign-in.",
                obj.user.email,
                password,
            ),
        )

    def _issue(self, request, obj) -> None:
        password = temporary_password()
        obj.user.set_password(password)
        obj.user.save(update_fields=["password"])
        obj.must_change_password = True
        obj.save(update_fields=["must_change_password"])
        self._show_password(request, obj, password)

    @admin.action(description=_("Issue a new temporary password"))
    def issue_password(self, request, queryset):
        for obj in queryset.select_related("user"):
            self._issue(request, obj)

    @action(
        description=_("New temporary password"),
        url_path="issue-password",
        icon="key",
        dialog={
            "title": _("Issue a new temporary password"),
            "description": _(
                "The donor's current password stops working at once. The new one is shown once; the donor "
                "must change it at the next sign-in."
            ),
            "form_class": ConfirmForm,
            "form_submit_text": _("Issue"),
        },
    )
    def issue_password_detail(self, request, form, object_id):
        obj = get_object_or_404(DonorAccount.objects.select_related("user"), pk=object_id)
        self._issue(request, obj)
        url = reverse("admin:donors_donoraccount_change", args=[obj.pk])
        if request.headers.get("HX-Request"):  # the dialog posts with HTMX: redirect the whole page
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)

    @admin.action(description=_("Switch off the selected accounts"))
    def switch_off(self, request, queryset):
        for obj in queryset.select_related("user"):
            obj.active = False
            obj.save(update_fields=["active"])
            obj.user.is_active = False
            obj.user.save(update_fields=["is_active"])
        messages.success(request, _("Switched off: those donors can no longer sign in."))

    @admin.display(description=_("Sign-in"), ordering="user__email")
    def sign_in(self, obj):
        return obj.user.email or obj.user.get_username()

    @admin.display(description=_("Donors"))
    def donor_list(self, obj):
        text = ", ".join(obj.donors)
        return text + (f" · {len(obj.grants)} grant(s)" if obj.grants else "")

    @admin.display(description=_("State"))
    def state(self, obj):
        if not obj.is_valid_now():
            return badge(_("Off") if not obj.active else _("Expired"), "bad")
        if obj.must_change_password:
            return badge(_("Waiting for first sign-in"), "warn")
        return badge(_("Active"), "ok")

    @admin.display(description=_("Preview"))
    def preview(self, obj):
        if not obj.pk:
            return "—"
        return format_html(
            '<a href="{}?account={}" target="_blank" rel="noopener">{}</a>',
            reverse("donors:page"),
            obj.pk,
            _("Open the donor's page"),
        )
