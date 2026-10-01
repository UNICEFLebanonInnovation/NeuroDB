from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from neurodb.accounts.models import Section

from .models import EXTENSIONS, MAX_FILE_MB, Document

MAX_PASTED_CHARS = 1_000_000


class DocumentForm(forms.ModelForm):
    file = forms.FileField(
        required=False,
        label=_("Document"),
        help_text=_("PDF, Word, PowerPoint, Excel, CSV or text, %(mb)s MB at most") % {"mb": MAX_FILE_MB},
        widget=forms.ClearableFileInput(attrs={"accept": ",".join(f".{e}" for e in EXTENSIONS)}),
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
        fields = ("title", "file", "text", "source", "section", "year")
        widgets = {"year": forms.NumberInput(attrs={"min": 1990, "max": 2100})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-select" if name == "section" else "form-control")

    def clean_year(self):
        year = self.cleaned_data.get("year")
        if year is not None and not 1990 <= year <= 2100:
            raise forms.ValidationError(_("Enter a year between 1990 and 2100."))
        return year

    def clean(self):
        cleaned = super().clean()
        has_file, has_text = bool(cleaned.get("file")), bool((cleaned.get("text") or "").strip())
        if has_file == has_text:
            raise forms.ValidationError(_("Choose a document or paste a text (one of the two)."))
        return cleaned
