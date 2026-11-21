"""Core arithmetic helpers.

Kept small on purpose: every function is reachable from the tests, so a patch
either changes behaviour or changes nothing.
"""

MAX_RANGE = 100


def add(a, b):
    """Return the sum of two numbers."""
    return a + b


def mean(values):
    """Return the arithmetic mean of a non-empty sequence."""
    if not values:
        raise ValueError("mean() of an empty sequence")
    return sum(values) / len(values)


def median(values):
    """Return the median; for an even count, the mean of the two middle values."""
    if not values:
        raise ValueError("median() of an empty sequence")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def parse_range(text):
    """Parse "low-high" into an inclusive list of integers."""
    start, _, end = text.partition("-")
    low, high = int(start), int(end)
    if high < low:
        raise ValueError("range end is before start")
    if high - low >= MAX_RANGE:
        raise ValueError("range is too large")
    return list(range(low, high + 1))


def slug(text):
    """Lower-case, whitespace-collapsed, hyphenated form of a phrase."""
    return "-".join(part for part in text.lower().split() if part)
