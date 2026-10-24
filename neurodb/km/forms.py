from django import forms

from . import words


class UploadForm(forms.Form):
    workbook = forms.FileField(label=words.UPLOAD_LABEL)
    again = forms.BooleanField(required=False, label=words.AGAIN_LABEL)
    fewer = forms.BooleanField(required=False, label=words.FEWER_LABEL)
