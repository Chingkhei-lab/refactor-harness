"""Command line front end for the calculator."""

import argparse
import sys

from .core import mean, median, parse_range, slug


def build_parser():
    parser = argparse.ArgumentParser(prog="calc", description="Small calculator.")
    parser.add_argument("--uppercase", action="store_true", help="upper-case the output")
    sub = parser.add_subparsers(dest="command", required=True)

    mean_parser = sub.add_parser("mean", help="mean of the given numbers")
    mean_parser.add_argument("values", nargs="+", type=float)

    median_parser = sub.add_parser("median", help="median of the given numbers")
    median_parser.add_argument("values", nargs="+", type=float)

    range_parser = sub.add_parser("range", help="inclusive integer range")
    range_parser.add_argument("bounds", help="low-high, e.g. 2-4")

    slug_parser = sub.add_parser("slug", help="hyphenate a phrase")
    slug_parser.add_argument("words", nargs="+")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "mean":
        result = str(mean(args.values))
    elif args.command == "median":
        result = str(median(args.values))
    elif args.command == "range":
        result = " ".join(str(value) for value in parse_range(args.bounds))
    else:
        result = slug(" ".join(args.words))
    if args.uppercase:
        result = result.upper()
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
