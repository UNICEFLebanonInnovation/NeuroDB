from django.contrib.admin import SimpleListFilter

class JSONFieldFilter(SimpleListFilter):
    """
    """

    def __init__(self, *args, **kwargs):

        super(JSONFieldFilter, self).__init__(*args, **kwargs)

        assert hasattr(self, 'title'), (
            'Class {} missing "title" attribute'.format(self.__class__.__name__)
        )
        assert hasattr(self, 'parameter_name'), (
            'Class {} missing "parameter_name" attribute'.format(self.__class__.__name__)
        )
        assert hasattr(self, 'json_field_name'), (
            'Class {} missing "json_field_name" attribute'.format(self.__class__.__name__)
        )
        assert hasattr(self, 'json_field_property_name'), (
            'Class {} missing "json_field_property_name" attribute'.format(self.__class__.__name__)
        )

    def lookups(self, request, model_admin):
        """
        # Improvemnt needed: if the size of jsonfield is large and there are lakhs of row
        """
        field_value_set = []
        if '__' in self.json_field_property_name:  # NOTE: this will cover only one nested level
            keys = self.json_field_property_name.split('__')
            field_value_set = set(
                data[keys[0]][keys[1]] for data in model_admin.model.objects.values(self.json_field_name)
            )
        else:
            try:

                field_value_set = set(
                   data[self.json_field_property_name] for data in model_admin.model.objects.values_list(self.json_field_name, flat=True)
                )
            except:
                # ZS: The above does not work when the json value is a LIST
                for ds in model_admin.model.objects.values_list(self.json_field_name, flat=True):
                    for d in ds:
                        field_value_set.append(d[self.json_field_property_name])
                field_value_set = set(field_value_set)
        return sorted([(v, v) for v in field_value_set])

    def queryset(self, request, queryset):
        if self.value():
            # try:
            #     json_field_query = {"{}__{}".format(self.json_field_name, self.json_field_property_name): self.value()}
            # except:
                # ZS: The above does not work when the json value is a LIST
            json_field_query = {"{}__0__{}".format(self.json_field_name, self.json_field_property_name): self.value()}
            return queryset.filter(**json_field_query)
        else:
            return queryset
