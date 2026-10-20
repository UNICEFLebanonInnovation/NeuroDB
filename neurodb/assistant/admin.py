from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.formats import date_format
from django.utils.html import escape
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.forms import BaseDialogForm
from unfold.widgets import UnfoldAdminPasswordWidget, UnfoldAdminSelectWidget, UnfoldBooleanWidget

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import llm, usage
from .llm import keys
from .models import AISettings, AIUsage, AssistantQuestion

TONES = {"answered": "ok", "refused": "warn", "failed": "bad", "limited": "muted"}


@admin.register(AssistantQuestion)
class AssistantQuestionAdmin(ReadOnlyModelAdmin):
    list_display = ("created_at", "user", "question_short", "status_badge", "lookups", "tokens", "duration")
    list_filter = ("status", "created_at", "model")
    search_fields = ("question", "answer", "user__username")
    date_hierarchy = "created_at"
    list_select_related = ("user",)
    list_per_page = 50

    @admin.display(description=_("Question"))
    def question_short(self, obj):
        return obj.question[:100] + ("…" if len(obj.question) > 100 else "")

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.get_status_display(), TONES.get(obj.status))

    @admin.display(description=_("Lookups"))
    def lookups(self, obj):
        return len(obj.tools or [])

    @admin.display(description=_("Tokens in / out"))
    def tokens(self, obj):
        return f"{obj.input_tokens + obj.cache_read_tokens:,} / {obj.output_tokens:,}"

    @admin.display(description=_("Time"), ordering="duration_ms")
    def duration(self, obj):
        return f"{obj.duration_ms / 1000:.1f} s"


class FeatureFilter(admin.SimpleListFilter):
    title = _("Feature")
    parameter_name = "feature"

    def lookups(self, request, model_admin):
        return list(usage.FEATURES.items())

    def queryset(self, request, queryset):
        return queryset.filter(feature=self.value()) if self.value() else queryset


@admin.register(AIUsage)
class AIUsageAdmin(ReadOnlyModelAdmin):
    """The AI use of every feature on the shared OpenAI key, per day, in tokens; in US dollars too
    when the optional AI_PRICE_* settings give the prices."""

    list_display = (
        "day", "feature_name", "model", "calls", "prompt_tokens", "cached", "output", "total",
    )  # fmt: skip
    list_filter = (FeatureFilter, "day", "model")
    date_hierarchy = "day"
    list_per_page = 100

    def get_list_display(self, request):
        shown = super().get_list_display(request)
        return (*shown, "cost") if usage.prices() else shown

    @admin.display(description=_("Feature"), ordering="feature")
    def feature_name(self, obj):
        return usage.FEATURES.get(obj.feature, obj.feature)

    @admin.display(description=_("Prompt tokens (not cached)"), ordering="input_tokens")
    def prompt_tokens(self, obj):
        return f"{obj.input_tokens:,}"

    @admin.display(description=_("Cached prompt tokens"), ordering="cached_tokens")
    def cached(self, obj):
        return f"{obj.cached_tokens:,}"

    @admin.display(description=_("Output tokens"), ordering="output_tokens")
    def output(self, obj):
        return f"{obj.output_tokens:,}"

    @admin.display(description=_("Total tokens"))
    def total(self, obj):
        return f"{obj.total_tokens:,}"

    @admin.display(description=_("Cost (USD)"))
    def cost(self, obj):
        price = usage.prices()
        if not price:
            return "-"
        dollars = usage.cost(obj, price)
        return "< $0.01" if 0 < dollars < usage.CENT else f"${dollars:,.2f}"


# ------------------------------------------------------------------------------------------- AI settings
KEY_MIN_CHARS = 20
# Each provider's fields: (provider, its key's name, the hosting team's setting, the row's prefix)
KEYS = (
    (llm.CLAUDE, "Anthropic", "ANTHROPIC_API_KEY", "anthropic"),
    (llm.OPENAI, "OpenAI", "OPENAI_API_KEY", "openai"),
)
CONSOLES = {
    llm.CLAUDE: _(
        "No key yet. Create one in the Claude Console (platform.claude.com, API keys) for the organisation's "
        "workspace."
    ),
    llm.OPENAI: _("No key yet. Create one on platform.openai.com, in the NeuroDB project's API keys."),
}


def _choices(default: str, values) -> list[tuple[str, str]]:
    return [("", default), *((value, value) for value in values)]


def _claude_models() -> list[tuple[str, str]]:
    # the models whose request NeuroDB sends (the price table's choice column), so a choice here cannot make
    # every call fail; CLAUDE_MODEL, set by the hosting team, may name another
    return [
        ("", _("The default: %(model)s (CLAUDE_MODEL)") % {"model": settings.CLAUDE_MODEL}),
        *((model, entry["name"]) for model, entry in usage.CLAUDE_PRICES.items() if entry["choice"]),
    ]


def _env_key(prefix: str) -> str:
    return settings.ANTHROPIC_API_KEY if prefix == "anthropic" else settings.OPENAI_API_KEY


class AISettingsForm(forms.ModelForm):
    """AI settings: the keys are written here only (never shown again, never echoed in a message) and are
    sealed when the page is saved; the choices are this form's, so a new model or effort needs no
    migration."""

    provider = forms.ChoiceField(
        label=_("AI provider"),
        choices=(
            (
                llm.AUTO,
                _("Automatic: Claude when an Anthropic API key is available, else OpenAI (recommended)"),
            ),
            (llm.CLAUDE, llm.NAMES[llm.CLAUDE].label),
            (llm.OPENAI, llm.NAMES[llm.OPENAI].label),
        ),
        widget=UnfoldAdminSelectWidget,
        help_text=AISettings._meta.get_field("provider").help_text,
    )
    anthropic_key = forms.CharField(
        label=_("Anthropic API key"),
        required=False,
        widget=UnfoldAdminPasswordWidget(attrs={"autocomplete": "new-password"}),
    )
    remove_anthropic_key = forms.BooleanField(
        label=_("Remove the Anthropic key entered here"), required=False, widget=UnfoldBooleanWidget
    )
    claude_model = forms.ChoiceField(
        label=_("Claude model"),
        required=False,
        widget=UnfoldAdminSelectWidget,
        help_text=AISettings._meta.get_field("claude_model").help_text,
    )
    claude_effort = forms.ChoiceField(
        label=_("Claude's default effort"),
        required=False,
        widget=UnfoldAdminSelectWidget,
        help_text=AISettings._meta.get_field("claude_effort").help_text,
    )
    openai_key = forms.CharField(
        label=_("OpenAI API key"),
        required=False,
        widget=UnfoldAdminPasswordWidget(attrs={"autocomplete": "new-password"}),
    )
    remove_openai_key = forms.BooleanField(
        label=_("Remove the OpenAI key entered here"), required=False, widget=UnfoldBooleanWidget
    )
    openai_effort = forms.ChoiceField(
        label=_("OpenAI's default reasoning effort"),
        required=False,
        widget=UnfoldAdminSelectWidget,
        help_text=AISettings._meta.get_field("openai_effort").help_text,
    )

    class Meta:
        model = AISettings
        fields = (
            "provider",
            "claude_model",
            "claude_effort",
            "claude_fallbacks",
            "openai_model",
            "openai_effort",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["claude_model"].choices = _claude_models()
        self.fields["claude_effort"].choices = _choices(
            _("The default: %(effort)s (CLAUDE_EFFORT)") % {"effort": settings.CLAUDE_EFFORT},
            settings.CLAUDE_EFFORTS,
        )
        self.fields["openai_effort"].choices = _choices(
            _("The default: %(effort)s (AI_ASSISTANT_EFFORT)") % {"effort": settings.AI_ASSISTANT_EFFORT},
            settings.AI_ASSISTANT_EFFORTS,
        )
        row = self.instance
        for provider, _vendor, setting, prefix in KEYS:
            key_field, remove_field = self.fields[f"{prefix}_key"], self.fields[f"remove_{prefix}_key"]
            env = _env_key(prefix)
            sealed = getattr(row, f"{prefix}_key_sealed")
            if env:
                key_field.disabled = True
                key_field.help_text = _(
                    "Set by the hosting team (%(setting)s, ending …%(end)s): it comes before a key entered "
                    "here."
                ) % {"setting": setting, "end": escape(keys.last4(env))}  # help texts are shown as HTML
            elif sealed and keys.unseal(sealed) is None:
                key_field.help_text = _(
                    "The key entered here can no longer be read: the site's secret key changed. Enter the "
                    "key again."
                )
            elif sealed:
                set_at = getattr(row, f"{prefix}_key_set_at")
                key_field.help_text = _(
                    "A key ending …%(end)s was entered on %(day)s. Leave this empty to keep it; it is never "
                    "shown again."
                ) % {
                    "end": escape(getattr(row, f"{prefix}_key_last4")),
                    "day": date_format(timezone.localtime(set_at), "j M Y") if set_at else "-",
                }
            else:
                key_field.help_text = CONSOLES[provider]
            remove_field.disabled = not sealed

    def _key(self, name: str) -> str:
        value = (self.cleaned_data.get(name) or "").strip()
        if value and (len(value) < KEY_MIN_CHARS or any(ch.isspace() for ch in value)):
            # the key itself is never repeated in a message
            raise ValidationError(
                _("This is not an API key: a key is at least 20 characters long, with no spaces.")
            )
        return value

    def clean_anthropic_key(self):
        return self._key("anthropic_key")

    def clean_openai_key(self):
        return self._key("openai_key")

    def clean_openai_model(self):
        value = (self.cleaned_data.get("openai_model") or "").strip()
        if llm.provider_of(value) == llm.CLAUDE:
            raise ValidationError(_("This is a Claude model: choose it under Claude (Anthropic)."))
        return value

    def clean(self):
        data = super().clean()
        choice = data.get("provider")
        for provider, vendor, _setting, prefix in KEYS:
            entered = bool(data.get(f"{prefix}_key"))
            stored = bool(keys.unseal(getattr(self.instance, f"{prefix}_key_sealed")))
            removing = bool(data.get(f"remove_{prefix}_key")) and not entered
            if choice != provider or _env_key(prefix) or entered or (stored and not removing):
                continue
            if stored:  # its only key would go while it is the provider chosen
                other = llm.NAMES[llm.OPENAI if provider == llm.CLAUDE else llm.CLAUDE].vendor
                self.add_error(
                    f"remove_{prefix}_key",
                    _("This is the only %(vendor)s key: choose Automatic or %(other)s first.")
                    % {"vendor": vendor, "other": other},
                )
            else:
                self.add_error(
                    f"{prefix}_key",
                    _("Enter the %(vendor)s API key first, or choose Automatic.") % {"vendor": vendor},
                )
        return data


class TestConnectionForm(BaseDialogForm):
    """The confirmation step of "Test the connection"."""


@admin.register(AISettings)
class AISettingsAdmin(ModelAdmin):
    """Admin → Data and sync → "AI settings": one row, made when an administrator first opens the page.
    The provider, the keys (write-only: only their last 4 characters show), the models and default effort,
    the refusal fallback and "Test the connection". Values the hosting team set (AI_PROVIDER and the keys)
    come first and say so here. A save applies within seconds on every worker, with no restart."""

    form = AISettingsForm
    fieldsets = (
        (_("Provider"), {"fields": ("provider", "in_use_now")}),
        (
            _("Claude (Anthropic)"),
            {
                "fields": (
                    "anthropic_key",
                    "remove_anthropic_key",
                    "claude_model",
                    "claude_effort",
                    "claude_fallbacks",
                )
            },
        ),
        (_("OpenAI"), {"fields": ("openai_key", "remove_openai_key", "openai_model", "openai_effort")}),
        (_("Last connection test"), {"fields": ("checked_at", "check_result")}),
        (None, {"fields": ("updated_by", "updated_at")}),
    )
    readonly_fields = ("in_use_now", "checked_at", "check_result", "updated_by", "updated_at")
    actions_detail = ("test_connection",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        return redirect(reverse("admin:assistant_aisettings_change", args=[AISettings.load().pk]))

    @method_decorator(sensitive_post_parameters("anthropic_key", "openai_key"))
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        # a Django error report of a failed save never carries the keys typed in
        return super().changeform_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        now = timezone.now()
        for _provider, _vendor, _setting, prefix in KEYS:
            entered = form.cleaned_data.get(f"{prefix}_key")
            if entered:
                sealed, last4, set_at = keys.seal(entered), keys.last4(entered), now
            elif form.cleaned_data.get(f"remove_{prefix}_key"):
                sealed, last4, set_at = "", "", None
            else:  # kept: sealed again with the current secret key (it opened with one of the fallbacks)
                kept = getattr(obj, f"{prefix}_key_sealed")
                sealed = keys.reseal(kept) or kept
                last4, set_at = getattr(obj, f"{prefix}_key_last4"), getattr(obj, f"{prefix}_key_set_at")
            setattr(obj, f"{prefix}_key_sealed", sealed)
            setattr(obj, f"{prefix}_key_last4", last4)
            setattr(obj, f"{prefix}_key_set_at", set_at)
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description=_("In use now"))
    def in_use_now(self, obj):
        p = llm.provider()
        entered = llm.snapshot()
        claude = p == llm.CLAUDE
        chosen_model = entered.claude_model if claude else entered.openai_model
        chosen_effort = entered.claude_effort if claude else entered.openai_effort
        here = _("chosen here")
        said = _(
            "%(provider)s, model %(model)s (%(model_from)s), default effort %(effort)s (%(effort_from)s)"
        ) % {
            "provider": llm.names(p).label,
            "model": llm.default_model(p),
            "model_from": here if chosen_model else ("CLAUDE_MODEL" if claude else "AI_ASSISTANT_MODEL"),
            "effort": llm.default_effort(p),
            "effort_from": here if chosen_effort else ("CLAUDE_EFFORT" if claude else "AI_ASSISTANT_EFFORT"),
        }
        by = llm.chosen_by()
        if by == "hosting":
            why = _("set by the hosting team (AI_PROVIDER=%(value)s): it comes before the choice here.") % {
                "value": settings.AI_PROVIDER
            }
        elif by == "admin":
            why = _("chosen here.")
        elif claude:
            source = _("set by the hosting team") if settings.ANTHROPIC_API_KEY else _("entered here")
            why = _("chosen automatically, as an Anthropic key is %(source)s.") % {"source": source}
        else:
            why = _("chosen automatically, as no Anthropic key is available.")
        text = f"{said}: {why}"
        if not getattr(settings, "AI_ASSISTANT_SWITCH", True):
            text += " " + _(
                "The hosting team switched the AI off (AI_ASSISTANT_ENABLED=false): no AI feature runs."
            )
        elif not llm.available(p):
            text += " " + _("It has no API key yet, so the AI features are off.")
        return text

    @action(
        description=_("Test the connection"),
        url_path="test-connection",
        icon="network_check",
        permissions=["change"],
        dialog={
            "title": _("Test the connection"),
            "description": _(
                'Sends the AI provider in use one short message ("Reply with the word OK."), with no NeuroDB '
                "data, and says whether the key, the model and the credit work. It takes a few seconds and a "
                "few tokens, counted in AI use as the connection test."
            ),
            "form_class": TestConnectionForm,
            "form_submit_text": _("Test"),
        },
    )
    def test_connection(self, request, form, object_id):
        check = llm.test_connection()
        # an update, not a save: the snapshot of AI settings stays as it is
        AISettings.objects.filter(pk=AISettings.load().pk).update(
            checked_at=timezone.now(), check_result=check.text[: llm.CHECK_CHARS]
        )
        (messages.success if check.ok else messages.warning)(request, check.text)
        url = reverse("admin:assistant_aisettings_change", args=[AISettings.load().pk])
        if request.headers.get("HX-Request"):  # the dialog posts with HTMX: redirect the whole page
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)
