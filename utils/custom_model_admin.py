from django.contrib import admin
from django.db import  models

from django.forms import Textarea, NumberInput, model_to_dict, TextInput
from django.contrib.admin.utils import unquote
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

class CustomModelAdmin(admin.ModelAdmin):
    list_per_page = 20
    list_max_show_all = 500

    exclude = ['institute', 'sequence_number']

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['title'] = ''
        return super(CustomModelAdmin, self).changelist_view(request, extra_context=extra_context)

    class Meta:
        ordering = ['-id']

    formfield_overrides = {
        models.TextField: {'widget': Textarea(attrs={'rows': 2, 'class': 'vLargeTextField'})},
        # models.PositiveIntegerField: {'widget': TextInput(attrs={'size': 4, })},
        models.DecimalField: {'widget': NumberInput(attrs={'style': 'width:200px;', 'class': 'vTextField'})},
    }

    def get_form(self, request, obj=None, **kwargs):
        request._obj_ = obj
        return super(CustomModelAdmin, self).get_form(request, obj, **kwargs)

    def has_change_permission(self, request, obj=None):
        if request.GET.get('view_mode', False):
            return False
        else:
            return True

    def has_delete_permission(self, request, obj=None):
        return True

    def has_view_permission(self, request, obj=None):
        return True

    def change_view(self, request, object_id, form_url='', extra_context=None):
        obj = self.get_object(request, unquote(object_id))
        extra_context = extra_context or {}
        # if hasattr(obj, 'status'):
        #     try:
        #         title = '{} : {} - {}'.format(self.opts.verbose_name.upper(), obj, obj.get_status_display().upper())
        #     except:
        #         title = '{} : {} - {}'.format(self.opts.verbose_name.upper(), obj, obj.status.upper())
        # else:
        #     title = '{} : {}'.format(self.opts.verbose_name.upper(), obj)
        title = '{} : {}'.format(self.opts.verbose_name.upper(), obj)

        extra_context['title'] = title
        return super(CustomModelAdmin, self).change_view(request, object_id, form_url, extra_context)

    def view_link(self,obj):
        if obj:
            url = reverse('admin:{}_{}_change'.format(obj._meta.app_label,obj._meta.model_name,),args=(obj.pk,)) + "?view_mode=True"
            return format_html('<a href="{}" title={}><i class="fa fa-info"></i></a>',url, _('View'),)
    view_link.short_description = ''

    def edit_link(self,obj):
        if obj:
            url = reverse('admin:{}_{}_change'.format(obj._meta.app_label, obj._meta.model_name,),args=(obj.pk,))
            return format_html('<a href="{}" title={}><i class="far fa-edit"></i></a>',url, _('Edit'),)
    edit_link.short_description = ''

    def delete_link(self,obj):
        if obj:
            url = reverse('admin:{}_{}_delete'.format(obj._meta.app_label,obj._meta.model_name,),args=(obj.pk,))
            return format_html('<a href="{}" title={}><i class="fa fa-trash"></i></a>',url, _('Delete'),)
    delete_link.short_description = ''

