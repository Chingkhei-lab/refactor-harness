import pytest

from calc.core import add, mean, median, parse_range, slug


def test_add():
    assert add(2, 3) == 5


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_mean_of_empty_sequence_raises():
    with pytest.raises(ValueError):
        mean([])


def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_median_even_is_the_middle_average():
    assert median([1, 2, 3, 4]) == 2.5


def test_parse_range_is_inclusive():
    assert parse_range("2-4") == [2, 3, 4]


def test_parse_range_rejects_reversed_bounds():
    with pytest.raises(ValueError):
        parse_range("4-2")


def test_slug():
    assert slug("Hello  World") == "hello-world"
