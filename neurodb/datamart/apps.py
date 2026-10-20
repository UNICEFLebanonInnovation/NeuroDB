from django.apps import AppConfig


class DatamartConfig(AppConfig):
    name = "neurodb.datamart"
    label = "datamart"
    verbose_name = "eTools Datamart"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from neurodb.watch import people

        from . import query

        # the people the eTools records name: never sent by Ask, and taken out of every text
        if query.datamart_people not in people.EXTRA_SOURCES:
            people.EXTRA_SOURCES.append(query.datamart_people)
