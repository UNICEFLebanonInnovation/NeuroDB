import pytest


@pytest.fixture(autouse=True)
def _km_live_afresh():
    """Every test reads the version shown afresh (the page keeps it 10 seconds in the process), and the
    KM Database's people afresh (kept per version and list of corrections)."""
    from neurodb.km import authors
    from neurodb.km.models import forget_live

    forget_live()
    authors.forget()
    yield
    forget_live()
    authors.forget()
