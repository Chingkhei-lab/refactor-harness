"""The upstream-suite oracle for the canary target: three plain assertions."""

from canary import absolute, label


def test_absolute_keeps_positive():
    assert absolute(3) == 3


def test_absolute_flips_negative():
    assert absolute(-3) == 3


def test_label_covers_the_signs():
    assert label(-1) == "negative"
    assert label(0) == "zero"
    assert label(1) == "positive"
