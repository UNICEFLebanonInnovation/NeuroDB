import pytest


@pytest.fixture(autouse=True)
def _km_live_afresh():
    """Every test reads the version shown afresh (the page keeps it 10 seconds in the process)."""
    from neurodb.km.models import forget_live

    forget_live()
    yield
    forget_live()
