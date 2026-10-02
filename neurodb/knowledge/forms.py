from __future__ import annotations

import os

from django import forms
from django.utils.translation import gettext_lazy as _

from neurodb.accounts.models import Section

from .models import EXTENSIONS, MAX_FILE_MB, Document

MAX_PASTED_CHARS = 1_000_000


MAX_FILES = 60  # documents added at once


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, list | tuple):
            return [single(d, initial) for d in data if d]
        return [single(data, initial)] if data else []


class DocumentForm(forms.ModelForm):
    files = MultipleFileField(
        required=False,
        label=_("Document(s)"),
        help_text=_(
            "PDF, Word, PowerPoint, Excel, CSV or text, %(mb)s MB at most each. Several files can be chosen "
            "at once (e.g. every edition of a report); each becomes a document named after its file."
        )
        % {"mb": MAX_FILE_MB},
        widget=MultipleFileInput(attrs={"accept": ",".join(f".{e}" for e in EXTENSIONS)}),
    )
    text = forms.CharField(
        required=False,
        label=_("…or paste the text"),
        widget=forms.Textarea(attrs={"rows": 8}),
        max_length=MAX_PASTED_CHARS,
    )
    section = forms.ModelChoiceField(Section.objects.order_by("name"), required=False, label=_("Section"))

    class Meta:
        model = Document
        fields = ("title", "files", "text", "source", "section", "year", "periodic")
        widgets = {"year": forms.NumberInput(attrs={"min": 1990, "max": 2100})}
        labels = {"periodic": _("Periodic report")}
        help_texts = {
            "periodic": _(
                "An edition of a report issued again and again (snapshot, situation report, dashboard) whose "
                "name carries its number or date, e.g. 'UNICEF SNAPSHOT - 02 October-2026 - NUM-37'. NeuroDB "
                "keeps its figures by date, so Ask NeuroDB can compare editions, show trends and draw charts."
            )
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].required = False
        self.fields["title"].help_text = _("Left empty, the file's name is used.")
        for name, field in self.fields.items():
            css = {"section": "form-select", "periodic": "form-check-input"}.get(name, "form-control")
            field.widget.attrs.setdefault("class", css)

    def clean_year(self):
        year = self.cleaned_data.get("year")
        if year is not None and not 1990 <= year <= 2100:
            raise forms.ValidationError(_("Enter a year between 1990 and 2100."))
        return year

    def clean_files(self):
        files = self.cleaned_data.get("files") or []
        if len(files) > MAX_FILES:
            raise forms.ValidationError(_("Choose at most %(n)s files at once.") % {"n": MAX_FILES})
        for f in files:
            if os.path.splitext(f.name)[1].lower().lstrip(".") not in EXTENSIONS:
                raise forms.ValidationError(
                    _("%(name)s: this kind of file cannot be read.") % {"name": f.name}
                )
            if f.size > MAX_FILE_MB * 1024 * 1024:
                raise forms.ValidationError(
                    _("%(name)s is larger than %(mb)s MB.") % {"name": f.name, "mb": MAX_FILE_MB}
                )
        return files

    def clean(self):
        cleaned = super().clean()
        has_file, has_text = bool(cleaned.get("files")), bool((cleaned.get("text") or "").strip())
        if has_file == has_text:
            raise forms.ValidationError(_("Choose a document or paste a text (one of the two)."))
        if has_text and not (cleaned.get("title") or "").strip():
            self.add_error("title", _("Give the text a title."))
        return cleaned
