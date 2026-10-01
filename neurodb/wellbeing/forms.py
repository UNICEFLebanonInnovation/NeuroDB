from __future__ import annotations

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

FOLLOW_UP_TYPES = (
    ("Phone call", _("Phone call")),
    ("Home visit", _("Home visit")),
    ("Caregiver visited the center", _("Caregiver visited the centre")),
    ("Talked with the partner", _("Talked with the partner")),
    ("Checked the records", _("Checked the records")),
)
RESULTS = (
    ("reached_returning", _("Child/caregiver reached: child is returning or continuing")),
    ("reached_support", _("Child/caregiver reached: extra support planned at the centre")),
    ("referred", _("Referred to a specialised service (CP, health, other)")),
    ("already_handled", _("Already handled (referral or follow-up done, not recorded)")),
    ("not_reached", _("Could not reach the child or caregiver")),
    ("dropped_out", _("Child has left the programme")),
    ("not_needed", _("Not needed: the data was wrong")),
)


class FollowUpForm(forms.Form):
    follow_up_type = forms.ChoiceField(choices=FOLLOW_UP_TYPES, label=_("How"))
    result = forms.ChoiceField(choices=RESULTS, label=_("Result"))
    followed_up_on = forms.DateField(label=_("Date"), widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(
        label=_("Note (optional; no names or sensitive details)"),
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, flag=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.flag = flag
        self.fields["followed_up_on"].initial = timezone.localdate()
        for name, field in self.fields.items():
            field.widget.attrs.setdefault(
                "class", "form-select" if name in ("follow_up_type", "result") else "form-control"
            )

    def clean_followed_up_on(self):
        day = self.cleaned_data["followed_up_on"]
        if day > timezone.localdate():
            raise forms.ValidationError(_("The date is in the future."))
        if self.flag is not None and day < self.flag.opened_on:
            raise forms.ValidationError(_("The date is before the flag was raised."))
        return day
