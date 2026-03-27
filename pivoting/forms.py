from django.forms.models import BaseInlineFormSet

class IndicatorFormSet(BaseInlineFormSet):
    def get_form_kwargs(self, index):
        kwargs = super(IndicatorFormSet, self).get_form_kwargs(index)
        kwargs['parent_object'] = self.instance
        return kwargs


