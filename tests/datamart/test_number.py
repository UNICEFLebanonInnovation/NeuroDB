"""Reading PRP values: never glue separate numbers into one."""

import pytest

from neurodb.datamart.monitoring import number


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        (1234, 1234.0),
        ("1,234", 1234.0),
        ("12.5 %", 12.5),
        ("-3", -3.0),
        ({"v": 1200, "d": 1, "c": 1200}, 1200.0),
        ("{'c': 1200.0, 'd': 1, 'v': 1200}", 1200.0),
        ('{"v": 45, "d": 100}', 0.45),
        ("{'v': 45, 'd': 1}", 45.0),
        ("45/100", None),
        ("2026-06-30", None),
        ("not a number", None),
        ("{broken", None),
        (True, None),
    ],
)
def test_number(raw, expected):
    assert number(raw) == expected
