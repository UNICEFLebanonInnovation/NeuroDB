from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Office, Section, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "section", "is_active", "is_staff")
    list_filter = ("is_active", "is_staff", "groups", "section")
    search_fields = ("username", "email", "first_name", "last_name")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("NeuroDB", {"fields": ("section", "backup_user", "skype_account")}),
    )


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "powerbi_url", "have_hpm_indicator")
    search_fields = ("name", "code")


@admin.register(Office)
class OfficeAdmin(admin.ModelAdmin):
    list_display = ("name",)
