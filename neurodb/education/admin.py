"""The education figures read from Compiler (read-only), and the comparison with Power BI's views: the views'
masked definitions and the card versions, for Administrators only."""

from __future__ import annotations

from django import forms
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from neurodb.accounts.roles import ADMIN, role_of
from neurodb.core import compute
from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import power_bi
from .figures import Figures
from .models import EducationFigures, PowerBiCard, PowerBiCheck, PowerBiView


def is_admin(request) -> bool:
    """An Administrator (the role, or a superuser): the only ones who see the views' definitions and choose
    the card versions."""
    return request.user.is_authenticated and role_of(request.user) == ADMIN


def counted_from(payload: dict) -> str:
    """'The view' or "Compiler's rule" from a payload's ``power_bi`` block, as a badge; "—" for a payload of
    an older Compiler (not compared)."""
    mode = (((power_bi.block(payload or {}) or {}).get("base")) or {}).get("mode")
    if mode == "view":
        return badge(_("the view"), "ok")
    if mode:
        return badge(_("Compiler's rule"), "warn")
    return "—"


def difference(payload: dict) -> str:
    """What was counted minus the view's registrations (the reconciliation's totals); "—" when unknown."""
    totals = (((power_bi.block(payload or {}) or {}).get("reconciliation")) or {}).get("totals") or {}
    counted, view = totals.get("counted"), totals.get("view_ids")
    if not isinstance(counted, int) or not isinstance(view, int):
        return "—"
    return f"{counted - view:+,}" if counted != view else "0"


@admin.register(EducationFigures)
class EducationFiguresAdmin(ReadOnlyModelAdmin):
    list_display = ("programme_label", "year", "children", "counted_from", "difference", "counted_at",
                    "fetched_at", "problems", "page")  # fmt: skip
    list_filter = ("programme",)
    fields = list_display
    readonly_fields = fields

    @admin.display(description=_("Programme"), ordering="programme")
    def programme_label(self, obj):
        return obj.payload.get("label") or obj.programme

    @admin.display(description=_("Children registered"))
    def children(self, obj):
        return Figures(obj.payload).block("registrations").total({}).get("people", 0)

    @admin.display(description=_("Counted from"))
    def counted_from(self, obj):
        return counted_from(obj.payload)

    @admin.display(description=_("Difference with the view"))
    def difference(self, obj):
        return difference(obj.payload)

    @admin.display(description=_("Parts not counted"))
    def problems(self, obj):
        failed = [n for n, b in obj.payload.get("blocks", {}).items() if b.get("error")]
        return ", ".join(failed) or "—"

    @admin.display(description=_("Page"))
    def page(self, obj):
        hidden = compute.per_request(  # read once per request (a dozen rows)
            "education:hidden-periods", lambda: power_bi.hidden_periods(power_bi.EDUCATION)
        )
        if obj.year in hidden.get(obj.programme, set()):
            return _("Not on the page: replaced by the figures Compiler sends now")
        url = reverse("education:dirasa" if obj.programme == "bridging" else "education:makani")
        kind = power_bi.period_kind(obj.programme, (obj.payload or {}).get("format"))
        return format_html('<a href="{}?{}">{}</a>', url, urlencode({kind: obj.year}), _("Open"))


class AdministratorsOnly:
    """Listed and opened by Administrators only."""

    def has_module_permission(self, request):
        return is_admin(request)

    def has_view_permission(self, request, obj=None):
        return is_admin(request)


@admin.register(PowerBiView)
class PowerBiViewAdmin(AdministratorsOnly, ReadOnlyModelAdmin):
    """The views Power BI reads, and their related views, as Compiler last described them: the definition
    with its string literals and long lists of numbers masked by Compiler (no name, no list of ids), its
    columns, the relations it reads and the hash of the raw text, with the date it last changed. Read-only."""

    list_display = ("name", "state", "kind", "can_select", "changed_at", "checked_at")
    list_filter = ("exists", "kind")
    search_fields = ("name",)
    readonly_fields = ("name", "state", "kind", "can_select", "column_list", "read_list", "masked_definition",
                       "sha256", "previous_sha256", "first_seen", "changed_at", "checked_at")  # fmt: skip
    fieldsets = (
        (None, {"fields": ("name", "state", "kind", "can_select", "first_seen", "changed_at", "checked_at")}),
        (_("Columns and relations read"), {"fields": ("column_list", "read_list")}),
        (_("Definition, masked by Compiler"), {"classes": ("collapse",), "fields": ("masked_definition",)}),
        (_("Hashes of the raw definition"), {"fields": ("sha256", "previous_sha256")}),
    )

    @admin.display(description=_("State"), ordering="exists")
    def state(self, obj):
        return badge(_("exists"), "ok") if obj.exists else badge(_("missing"), "bad")

    @admin.display(description=_("Columns"))
    def column_list(self, obj):
        rows = [c for c in obj.columns or [] if isinstance(c, list | tuple) and c]
        if not rows:
            return "—"
        return format_html_join(
            ", ", "<code>{}</code> {}", ((c[0], c[1] if len(c) > 1 else "") for c in rows)
        )

    @admin.display(description=_("Reads"))
    def read_list(self, obj):
        rows = [r for r in obj.reads or [] if isinstance(r, list | tuple) and len(r) >= 2]
        if not rows:
            return "—"
        return format_html_join(
            ", ", "<code>{}.{}</code> {}", ((r[0], r[1], f"({r[2]})" if len(r) > 2 else "") for r in rows)
        )

    @admin.display(description=_("Definition"))
    def masked_definition(self, obj):
        if not obj.definition:
            return "—"
        return format_html('<pre class="nd-json" style="white-space: pre-wrap">{}</pre>', obj.definition)


def _card_names() -> list[tuple[str, str]]:
    names: dict[str, str] = {}
    for cards in power_bi.VARIANTS.values():
        for name, definition in cards.items():
            names.setdefault(name, str(definition.label or name))
    return sorted(names.items())


def _period_label(period: str) -> str:
    return str(_("All years")) if period == power_bi.ALL else period


def compared_periods(programme: str | None) -> list[str]:
    """The periods Data health compares for ``programme`` (every programme's when None), latest first and all
    years together last: what a Power BI number can be typed for. Makani's are years ("2026"), Dirasa's are
    years (payload format 4, "2026") or round names (older payloads, "Bridging 2026-2027"), youth's years and
    "all"; each programme's latest first as its page orders them, the periods its page no longer lists left
    out (:func:`neurodb.education.power_bi.latest_first`: a Dirasa round by its start, not its name)."""
    checks = PowerBiCheck.objects.all()
    if programme:
        checks = checks.filter(programme=programme)
    rows = list(checks.values_list("programme", "period"))
    stored = power_bi.stored_periods({p for p, _period in rows})
    out: list[str] = []
    for name in dict.fromkeys([p for p, _l, _u in power_bi.PROGRAMMES] + sorted({p for p, _period in rows})):
        periods = (period for p, period in rows if p == name)
        out += power_bi.latest_first(name, periods, stored.listed, stored.hidden)
    every = [power_bi.ALL] if any(period == power_bi.ALL for _p, period in rows) else []
    return list(dict.fromkeys(out)) + every


class PowerBiCardForm(forms.ModelForm):
    """The card's version (one of :data:`neurodb.education.power_bi.VARIANTS`), Power BI's number and the
    period it was read for: one of the periods Data health compares (Dirasa: a year such as 2026; a round by
    its name in counts made before Compiler counted Dirasa by year), chosen in a list rather than typed, so
    that it always matches one; left empty, the latest period."""

    class Meta:
        model = PowerBiCard
        fields = ("programme", "card", "variant", "powerbi_value", "powerbi_period")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "programme" in self.fields:
            self.fields["programme"] = forms.ChoiceField(
                label=_("Programme"), choices=[(p, label) for p, label, _url in power_bi.PROGRAMMES]
            )
        if "card" in self.fields:
            self.fields["card"] = forms.ChoiceField(label=_("Card"), choices=_card_names())
        definition = power_bi.VARIANTS.get(self.instance.programme, {}).get(self.instance.card)
        if self.instance.pk and definition is not None:
            choices = [(v, definition.names.get(v, v)) for v in definition.variants]
        else:
            choices = sorted(
                {(v, v) for cards in power_bi.VARIANTS.values() for d in cards.values() for v in d.variants}
            )
        self.fields["variant"] = forms.ChoiceField(
            label=_("Version"),
            choices=choices,
            help_text=_(
                "The definition the pages use for this card; Data health shows what each one counts."
            ),
        )
        periods = compared_periods(self.instance.programme if self.instance.pk else None)
        period_choices = [("", _("The latest period"))] + [(p, _period_label(p)) for p in periods]
        current = self.instance.powerbi_period
        if current and current not in periods:  # typed before the list existed: shown, to be chosen again
            label = _("%(period)s (not a period Data health compares)") % {"period": current}
            period_choices.append((current, label))
        self.fields["powerbi_period"] = forms.ChoiceField(
            label=_("Period of Power BI's number"),
            choices=period_choices,
            required=False,
            help_text=_(
                "The period Power BI shows the number for, as Data health lists it (Dirasa: a year such as "
                "2026; a round by its name in counts made before Compiler counted Dirasa by year). Left "
                "empty: the latest period."
            ),
        )

    def clean(self):
        data = super().clean()
        programme = data.get("programme") or self.instance.programme
        card = data.get("card") or self.instance.card
        definition = power_bi.VARIANTS.get(programme, {}).get(card)
        if definition is None:
            raise forms.ValidationError(_("This programme has no such card."))
        if data.get("variant") not in definition.variants:
            raise forms.ValidationError(
                _("Choose one of this card's versions: %(versions)s.")
                % {"versions": ", ".join(definition.variants)}
            )
        periods = compared_periods(programme)
        period = data.get("powerbi_period") or ""
        if period and period not in periods:
            self.add_error(
                "powerbi_period",
                _("Choose one of the periods Data health compares for this programme: %(periods)s.")
                % {"periods": ", ".join(_period_label(p) for p in periods) or "—"},
            )
        elif not period and data.get("powerbi_value") is not None and periods:
            data["powerbi_period"] = power_bi.latest_period(programme) or ""  # the latest period's number
        return data


@admin.register(PowerBiCard)
class PowerBiCardAdmin(AdministratorsOnly, ModelAdmin):
    """The version of each card the pages show (Makani, Dirasa, youth, and the country programme's children
    enrolled), and the number Power BI shows for it. Type the number Power BI shows for a card and period:
    Data health (*Compared with Power BI*) marks the version that gives it; choose that version here, and
    the pages show it at once. Every card is listed, with its default, the first time this list is opened."""

    form = PowerBiCardForm
    list_display = ("programme_label", "card_label", "version", "powerbi_value", "powerbi_period", "equals",
                    "noted_at", "noted_by")  # fmt: skip
    list_filter = ("programme",)
    list_select_related = ("noted_by",)

    def has_add_permission(self, request):
        return is_admin(request)

    def has_change_permission(self, request, obj=None):
        return is_admin(request)

    def has_delete_permission(self, request, obj=None):
        return is_admin(request)

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.pk:
            return ("programme", "card", "noted_at", "noted_by", "versions")
        return ("noted_at", "noted_by")

    def get_fields(self, request, obj=None):
        fields = ["programme", "card", "variant", "powerbi_value", "powerbi_period", "noted_at", "noted_by"]
        return [*fields, "versions"] if obj is not None and obj.pk else fields

    def changelist_view(self, request, extra_context=None):
        if is_admin(request):
            ensure_cards()
        return super().changelist_view(request, extra_context)

    def save_model(self, request, obj, form, change):
        if not change or form.has_changed():
            obj.noted_at, obj.noted_by = timezone.now(), request.user
        super().save_model(request, obj, form, change)

    @admin.display(description=_("Programme"), ordering="programme")
    def programme_label(self, obj):
        return {p: label for p, label, _url in power_bi.PROGRAMMES}.get(obj.programme, obj.programme)

    @admin.display(description=_("Card"), ordering="card")
    def card_label(self, obj):
        definition = power_bi.VARIANTS.get(obj.programme, {}).get(obj.card)
        return definition.label if definition else obj.card

    @admin.display(description=_("Version in use"))
    def version(self, obj):
        definition = power_bi.VARIANTS.get(obj.programme, {}).get(obj.card)
        if definition is None:
            return obj.variant
        if obj.variant not in definition.variants:
            return format_html("{} ({})", obj.variant, _("unknown: the default is used"))
        name = definition.names.get(obj.variant, obj.variant)
        if obj.variant == definition.default:
            return format_html("{} <span class='text-muted'>({})</span>", name, _("default"))
        return name

    def _values(self, obj) -> tuple[str, dict]:
        """The comparison of the card's programme the number is compared with (the period chosen, else the
        latest period, as Data health) and the versions' values in it."""
        period = obj.powerbi_period or power_bi.latest_period(obj.programme)
        found = None
        if period:
            found = PowerBiCheck.objects.filter(programme=obj.programme, period=period).first()
        if found is None:
            return "", {}
        return found.period, (found.variants or {}).get(obj.card) or {}

    @staticmethod
    def _unknown_period(obj) -> bool:
        """A period typed that Data health does not compare: no comparison of it, or one of a period the page
        no longer lists (an old Dirasa round once Compiler counts Dirasa by year: its comparison is kept)."""
        if not obj.powerbi_period:
            return False
        if obj.powerbi_period in power_bi.hidden_periods([obj.programme]).get(obj.programme, set()):
            return True
        return not PowerBiCheck.objects.filter(programme=obj.programme, period=obj.powerbi_period).exists()

    @admin.display(description=_("Versions equal to Power BI's number"))
    def equals(self, obj):
        if obj.powerbi_value is None:
            return "—"
        if self._unknown_period(obj):
            return badge(_("no such period: choose one"), "warn")
        period, values = self._values(obj)
        if not values:
            return _("Not compared yet")
        definition = power_bi.VARIANTS.get(obj.programme, {}).get(obj.card)
        hits = [v for v, value in values.items() if value == obj.powerbi_value]
        if not hits:
            return badge(_("none"), "bad")
        names = [str(definition.names.get(v, v)) if definition else v for v in hits]
        return format_html("{} <span class='text-muted'>({})</span>", "; ".join(names), period)

    @admin.display(description=_("Every version, at the period total"))
    def versions(self, obj):
        definition = power_bi.VARIANTS.get(obj.programme, {}).get(obj.card)
        if self._unknown_period(obj):
            return _(
                "Data health compares no period “%(period)s” for this programme: choose one of the periods "
                "in the list (Dirasa: a year such as 2026)."
            ) % {"period": obj.powerbi_period}
        period, values = self._values(obj)
        if definition is None or not values:
            return _("Not compared yet: the next run of the Compiler job counts them.")
        return format_html(
            "<p>{}</p><ul>{}</ul>",
            _("In %(period)s:") % {"period": period},
            format_html_join(
                "",
                "<li>{}: <strong>{}</strong></li>",
                (
                    (definition.names.get(v, v), "—" if values.get(v) is None else f"{values[v]:,}")
                    for v in definition.variants
                ),
            ),
        )


def ensure_cards() -> None:
    """A row for every card of :data:`neurodb.education.power_bi.VARIANTS`, with its default version, so the
    administrators find each card in the list (a dozen rows; nothing changes on the pages)."""
    have = set(PowerBiCard.objects.values_list("programme", "card"))
    missing = [
        PowerBiCard(programme=programme, card=card, variant=definition.default)
        for programme, cards in power_bi.VARIANTS.items()
        for card, definition in cards.items()
        if (programme, card) not in have
    ]
    if missing:
        PowerBiCard.objects.bulk_create(missing, ignore_conflicts=True)
