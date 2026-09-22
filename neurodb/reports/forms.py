"""Forms of the reports pages (HPM comments, saved views)."""

from __future__ import annotations

import json

from django import forms
from django.utils.translation import gettext_lazy as _

from neurodb.core.models import SavedView
from neurodb.indicators.models import NeuroReport, NeuroReportComment, NeuroReportMasterIndicator


class HPMCommentForm(forms.ModelForm):
    class Meta:
        model = NeuroReportComment
        fields = ("master", "comment", "related_month")
        widgets = {"comment": forms.Textarea(attrs={"rows": 3, "maxlength": 5000})}

    def __init__(self, report: NeuroReport, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.report = report
        self.fields["master"].queryset = NeuroReportMasterIndicator.objects.filter(
            report=report
        ).select_related("master__database")
        self.fields["master"].label = _("Indicator")
        self.fields["comment"].label = _("Comment")
        self.fields["comment"].required = True
        self.fields["related_month"].label = _("Month")

    def save(self, commit: bool = True) -> NeuroReportComment:
        self.instance.report = self.report
        return super().save(commit=commit)


class JSONField(forms.CharField):
    """A text field carrying a JSON object (from a form post or a JSON body)."""

    def to_python(self, value):
        if value in (None, ""):
            return {}
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise forms.ValidationError(_("Invalid JSON.")) from exc
        if not isinstance(parsed, dict):
            raise forms.ValidationError(_("Expected a JSON object."))
        return parsed


class SavedViewForm(forms.ModelForm):
    query = JSONField(required=False)
    layout = JSONField(required=False)

    class Meta:
        model = SavedView
        fields = ("name", "page", "object_id", "query", "layout", "is_shared")

    def clean_page(self) -> str:
        page = self.cleaned_data["page"]
        if not page.startswith("reports:"):
            raise forms.ValidationError(_("Unknown page."))
        return page
