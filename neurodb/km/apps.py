from django.apps import AppConfig


def place_words() -> list[str]:
    """The places of the KM Database's list (every governorate, district and town, the regions; not the
    camps, whose own names are family names too) written as one word in Latin letters: NeuroDB's e-mail rule
    reads them as places after "at" (``watch.people.PLACE_SOURCES``: "We met the NGO at Bebnine. Info sessions
    followed" is no address)."""
    from . import places

    every = [s for p in (*places.GOVERNORATES, *places.DISTRICTS) if not p.camp for s in p.spellings]
    every += list(places.REGIONS)
    return [s for s in every if not places.arabic(s) and len(s.split()) == 1]


class KMConfig(AppConfig):
    name = "neurodb.km"
    label = "km"
    verbose_name = "KM Database"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # the places of the KM Database's list join the places the e-mail rule knows (registered here, so that
        # no module outside the KM Database imports it)
        from neurodb.watch import people

        if place_words not in people.PLACE_SOURCES:
            people.PLACE_SOURCES.append(place_words)
