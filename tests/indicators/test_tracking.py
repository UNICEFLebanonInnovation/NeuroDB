import datetime

from neurodb.indicators.services.tracking import NO_TARGET, OFF_TRACK, ON_TRACK, OVER_TARGET, percentage_of_year_elapsed, tracking

MID_YEAR = datetime.date(2026, 7, 2)  # ~50% elapsed


def test_percentage_elapsed():
    assert 49 <= percentage_of_year_elapsed(2026, MID_YEAR) <= 51
    assert percentage_of_year_elapsed(2025, MID_YEAR) == 100.0
    assert percentage_of_year_elapsed(2027, MID_YEAR) == 0.0


def test_tracking_rule():
    assert tracking(500, 1000, 2026, MID_YEAR).status == ON_TRACK
    assert tracking(300, 1000, 2026, MID_YEAR).status == OFF_TRACK
    assert tracking(700, 1000, 2026, MID_YEAR).status == OVER_TARGET
    assert tracking(700, 0, 2026, MID_YEAR).status == NO_TARGET
    assert tracking(None, 1000, 2026, MID_YEAR).status == OFF_TRACK
