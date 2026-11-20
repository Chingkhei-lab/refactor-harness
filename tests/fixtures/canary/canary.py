"""A two-function target for the canaries: one breakable, one not."""


def absolute(value):
    """Return the distance of `value` from zero."""
    if value < 0:
        return -value
    return value


def label(value):
    """Return a word for the sign of `value`."""
    if value < 0:
        return "negative"
    if value == 0:
        return "zero"
    return "positive"
