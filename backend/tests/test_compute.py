import pytest

from app.compute import fibonacci


@pytest.mark.parametrize(
    ("n", "expected"),
    [(0, 0), (1, 1), (2, 1), (10, 55), (20, 6765), (25, 75025)],
)
def test_fibonacci_known_values(n, expected):
    assert fibonacci(n) == expected
