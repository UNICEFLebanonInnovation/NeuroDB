from django import forms
from django.contrib import admin

from .models import Map, Resource, ResourceTag, ResourceTopic, ResourceType


class ResourceForm(forms.ModelForm):
    """Upload a file into the v2 BinaryField columns (kept until files move to Blob storage)."""

    upload = forms.FileField(required=False, help_text="Document to publish")
    cover = forms.ImageField(required=False, help_text="Cover image")

    class Meta:
        model = Resource
        exclude = ("resource_file", "resource_image", "resource_file_name", "resource_image_name")

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("upload"):
            f = self.cleaned_data["upload"]
            obj.resource_file, obj.resource_file_name = f.read(), f.name
        if self.cleaned_data.get("cover"):
            f = self.cleaned_data["cover"]
            obj.resource_image, obj.resource_image_name = f.read(), f.name
        if commit:
            obj.save()
            self.save_m2m()
        return obj


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    form = ResourceForm
    list_display = ("title", "publication_year", "type", "topic", "section", "published")
    list_filter = ("published", "publication_year", "type", "topic")
    search_fields = ("title", "description")
    filter_horizontal = ("tags",)


for model in (ResourceType, ResourceTopic, ResourceTag):
    admin.site.register(model)


@admin.register(Map)
class MapAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "link", "modified")
    list_filter = ("status",)
