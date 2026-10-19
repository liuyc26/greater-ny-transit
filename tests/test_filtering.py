import pytest

from transit.filtering import classify_direction


@pytest.mark.parametrize(
    "first,last,min_core,max_core,expected",
    [
        (1, 10, None, None, None),   # never reaches Manhattan
        (1, 10, 9, 10, "inbound"),   # Harlem-125 then Grand Central
        (1, 10, 1, 2, "outbound"),   # Grand Central then Harlem-125
        (1, 10, 5, 5, "through"),    # passes through mid-trip
        (1, 10, 1, 10, "through"),   # core at both ends
        (3, 12, 12, 12, "inbound"),  # sequences need not start at 1
    ],
)
def test_classify_direction(first, last, min_core, max_core, expected):
    assert classify_direction(first, last, min_core, max_core) == expected
