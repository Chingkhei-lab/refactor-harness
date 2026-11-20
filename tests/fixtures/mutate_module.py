"""A tiny vendored module that every mutator operator can bite on.

The mutator test used to read `.harness/target/parse/__init__.py`, which is
git-ignored, so it skipped everywhere except a developer's machine — the one test
that proves the mutator produces valid code was the one test CI never ran. This
module exists so that test is hermetic: it is checked in, it is small, and it
deliberately contains one of everything the operators target.
"""

LIMIT = 3


def compare(a, b):
    if a < b:
        return -1
    if a == b:
        return 0
    found = a in (1, 2, 3)
    same = a is None
    return 1 if not (found or same) else 2


def arithmetic(a, b):
    total = a + b
    diff = a - b
    product = a * b
    quotient = a / b
    floor = a // b
    remainder = a % b
    return total, diff, product, quotient, floor, remainder


def constants(x):
    if x > LIMIT + 1:
        return x + 2
    while x < 10:
        x = x + 1
    return x - 3


def booleans(a, b):
    if a and b:
        return True
    if a or b:
        return False
    return not a


def returns(value):
    if value:
        return value
    return None


def slices(seq):
    head = seq[1:4]
    tail = seq[0:-1]
    step = seq[0:5:2]
    return head, tail, step


def blocks(seq):
    total = 0
    for item in seq:
        total += item
        if item:
            total += 1
        else:
            total -= 1
    return total


def call_chain(seq):
    return [len(item) for item in seq if item]
