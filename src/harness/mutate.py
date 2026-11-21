"""An AST mutator small enough to explain, one change per mutant.

Mutation testing answers a different question from a test run: not "do the tests
pass" but "would the tests notice if the code were wrong". The mutator is written
here rather than taken from a library for one reason — every operator has to be
explainable, and a survivor has to be attributable to a specific edit at a
specific line.

Seven operator families, each applied to ONE site (eight operators: the int +-1
family is enumerated in both directions, because a `+1`-only mutator cannot reach
an off-by-one that should have been a decrement):

    compare_swap     <  -> <=, == -> !=, is -> is not, in -> not in (and back)
    arith_swap       + <-> -, * <-> /, // <-> %
    int_plus         integer constant n -> n + 1
    int_minus        integer constant n -> n - 1
    bool_flip        and -> or, or -> and, and `not x` -> `x`
    return_none      `return <expr>` -> `return None`
    delete_stmt      delete one statement from a block
    slice_bound      a literal slice bound n -> n + 1

Sites are enumerated deterministically (a depth-first walk in source order), so a
seed plus a count reproduces a sample exactly on any machine.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

OPERATORS = (
    "compare_swap",
    "arith_swap",
    "int_plus",
    "int_minus",
    "bool_flip",
    "return_none",
    "delete_stmt",
    "slice_bound",
)

_COMPARE = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}

_ARITH = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mod,
    ast.Mod: ast.FloorDiv,
}

# Statements that may be deleted: expressions, assignments, returns, raises,
# passes. Control flow and definitions are excluded — deleting an `if` or a
# `try` changes the shape of the program rather than its logic, and the
# survivorship of those mutants says little about the case set.
_DELETABLE = (ast.Expr, ast.Assign, ast.AugAssign, ast.Return, ast.Raise, ast.Pass)


@dataclass(frozen=True, order=True)
class Site:
    """One place an operator can be applied, addressed by a path from the root."""

    line: int
    column: int
    operator: str
    path: tuple[int, ...]
    # Only used by `delete_stmt`: which statement list of the owner node
    # (`body` and `orelse` of one `if` are different blocks at the same path).
    field: str = ""

    @property
    def key(self) -> str:
        return f"{self.operator}@{self.line}:{self.column}"


def _node_at(tree: ast.AST, path: tuple[int, ...]) -> ast.AST:
    node: ast.AST = tree
    for index in path:
        node = list(ast.iter_child_nodes(node))[index]
    return node


def _is_bool_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, bool)


def enumerate_sites(source: str) -> list[Site]:
    """Every site, in a deterministic order, with no duplicates."""
    tree = ast.parse(source)
    sites: list[Site] = []

    def walk(node: ast.AST, path: tuple[int, ...]) -> None:
        line = getattr(node, "lineno", 0)
        column = getattr(node, "col_offset", 0)
        if isinstance(node, ast.Compare) and node.ops and type(node.ops[0]) in _COMPARE:
            sites.append(Site(line, column, "compare_swap", path))
        if isinstance(node, ast.BinOp) and type(node.op) in _ARITH:
            sites.append(Site(line, column, "arith_swap", path))
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not _is_bool_constant(node)
            and not isinstance(node.value, bool)
        ):
            sites.append(Site(line, column, "int_plus", path))
            sites.append(Site(line, column, "int_minus", path))
        if isinstance(node, ast.BoolOp):
            sites.append(Site(line, column, "bool_flip", path))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sites.append(Site(line, column, "bool_flip", path))
        if (
            isinstance(node, ast.Return)
            and node.value is not None
            and not (
                isinstance(node.value, ast.Constant) and node.value.value is None
            )
        ):
            # `return None` -> `return None` is not a mutant.
            sites.append(Site(line, column, "return_none", path))
        if isinstance(node, ast.Slice):
            for bound, name in ((node.lower, "slice_bound"), (node.upper, "slice_bound")):
                if isinstance(bound, ast.Constant) and isinstance(bound.value, int):
                    sites.append(Site(line, column, name, path))
        for index, child in enumerate(ast.iter_child_nodes(node)):
            walk(child, (*path, index))

    walk(tree, ())
    # Deleting a statement is addressed at its containing list, so it is
    # collected separately: a statement site is the parent block plus index.
    for parent_path, parent_field, parent in _blocks(tree, ()):
        for index, statement in enumerate(parent):
            # A block whose only statement is deleted would not parse, so that
            # is not a site: every enumerated site must yield a valid program.
            if len(parent) > 1 and isinstance(statement, _DELETABLE):
                sites.append(
                    Site(
                        getattr(statement, "lineno", 0),
                        getattr(statement, "col_offset", 0),
                        "delete_stmt",
                        (*parent_path, index),
                        parent_field,
                    )
                )
    unique = {(site.operator, site.path, site.field): site for site in sites}
    return sorted(unique.values())


def _blocks(
    tree: ast.AST, path: tuple[int, ...]
) -> list[tuple[tuple[int, ...], str, list[Any]]]:
    """Every statement list (module body, function body, if body, ...) with its path.

    Two separate loops on purpose: the first collects this node's statement
    lists, the second recurses **once per child**. Doing both in one loop
    recursed once per *field*, which is exponential in the nesting depth.
    """
    found: list[tuple[tuple[int, ...], str, list[Any]]] = []
    for field_name, value in ast.iter_fields(tree):
        if isinstance(value, list) and value and all(
            isinstance(item, ast.stmt) for item in value
        ):
            found.append((path, field_name, value))
    for index, child in enumerate(ast.iter_child_nodes(tree)):
        found.extend(_blocks(child, (*path, index)))
    return found


def _delete_from_block(tree: ast.AST, path: tuple[int, ...], field: str) -> bool:
    """`path` points at (block owner, index): delete that statement in place."""
    wanted = path[:-1]
    index = path[-1]
    for block_path, block_field, statements in _blocks(tree, ()):
        if block_path == wanted and block_field == field and 0 <= index < len(statements):
            del statements[index]
            return True
    return False


_SPELLING: dict[type, str] = {
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.And: "and",
    ast.Or: "or",
}


def _split_lines(data: bytes) -> list[bytes]:
    """Split on newlines only, the way `ast` numbers lines.

    `str.splitlines()` also breaks on form feed and U+2028, which would shift
    every offset after such a character and make a splice land in the wrong place.
    """
    parts = data.split(b"\n")
    lines = [part + b"\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _splice(source: str, start: tuple[int, int], end: tuple[int, int], text: str) -> str:
    """Replace the character range [start, end) with `text`, byte-exactly.

    `ast` column offsets are UTF-8 byte offsets, not character offsets, so the
    edit is done on encoded lines; a module with any non-ASCII character would
    otherwise be spliced one or two bytes off.
    """
    lines = _split_lines(source.encode("utf-8"))
    first, last = start[0], end[0]
    head = b"".join(lines[: first - 1]) + lines[first - 1][: start[1]]
    rest = lines[last - 1][end[1]:] + b"".join(lines[last:])
    return (head + text.encode("utf-8") + rest).decode("utf-8")


def _start(node: ast.AST) -> tuple[int, int]:
    """(line, byte column) where a node begins. `ast` leaves these off `AST`."""
    lineno, column = getattr(node, "lineno", None), getattr(node, "col_offset", None)
    assert lineno is not None and column is not None, node
    return (int(lineno), int(column))


def _end(node: ast.AST) -> tuple[int, int]:
    lineno = getattr(node, "end_lineno", None)
    column = getattr(node, "end_col_offset", None)
    assert lineno is not None and column is not None, node
    return (int(lineno), int(column))


def _text(source: str, start: tuple[int, int], end: tuple[int, int]) -> str:
    """The exact source text in [start, end)."""
    lines = _split_lines(source.encode("utf-8"))
    if start[0] == end[0]:
        return lines[start[0] - 1][start[1] : end[1]].decode("utf-8")
    middle = b"".join(lines[start[0] : end[0] - 1])
    return (
        lines[start[0] - 1][start[1] :] + middle + lines[end[0] - 1][: end[1]]
    ).decode("utf-8")


def _between(source: str, left: ast.AST, right: ast.AST) -> str:
    """The exact source text between two nodes (the operator's own text)."""
    return _text(source, _end(left), _start(right))


def _replace_between(
    source: str, left: ast.AST, right: ast.AST, before: type, after: type
) -> str:
    old, new = _SPELLING[before], _SPELLING[after]
    gap = _between(source, left, right)
    if old not in gap:
        raise ValueError(f"no {old!r} between operands; gap was {gap!r}")
    return _splice(source, _end(left), _start(right), gap.replace(old, new, 1))


def _drop_lines(source: str, first: int, last: int) -> str:
    """Delete whole lines `first..last` (1-based, inclusive)."""
    lines = _split_lines(source.encode("utf-8"))
    return b"".join(lines[: first - 1] + lines[last:]).decode("utf-8")


def _line_prefix(source: str, lineno: int, column: int) -> str:
    lines = _split_lines(source.encode("utf-8"))
    return lines[lineno - 1][:column].decode("utf-8")


def _changed_span(before: str, after: str) -> tuple[int, int, int]:
    """(first changed line, lines replaced, lines inserted) for one contiguous edit.

    Scanned from both ends, so it is O(n) with C-level comparisons: a whole-file
    reformat reports thousands of lines here, and one operator reports one.
    """
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    first = 0
    limit = min(len(old), len(new))
    while first < limit and old[first] == new[first]:
        first += 1
    tail = 0
    while tail < limit - first and old[len(old) - 1 - tail] == new[len(new) - 1 - tail]:
        tail += 1
    return (first + 1, len(old) - tail - first, len(new) - tail - first)


def _changed_regions(before: str, after: str) -> list[tuple[str, int, int]]:
    """Non-equal diff regions between two sources, as (tag, old lines, new lines).

    Used as the mutator's own invariant: one site, one contiguous edit. An
    `ast.unparse` round trip passes every other check — it parses, and it is a
    single operator change in the AST — while rewriting every line of the file,
    and a case that observes a traceback then reports *every* mutant as
    different. This invariant is what makes "exactly one change per mutant" true
    of the file, not just of the tree.
    """
    import difflib

    matcher = difflib.SequenceMatcher(
        None,
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        autojunk=False,
    )
    return [
        (tag, i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def apply_site(source: str, site: Site) -> str:
    """Return the mutated source: one minimal textual edit at `site`.

    The edit splices the original text and never re-prints the tree, so every
    line the mutation does not touch stays byte-identical. That matters beyond
    tidiness: a case may observe the target's own path, a traceback, or a warning
    that names a line, and a whole-file reformat makes all of those differ for
    *every* mutant, which reads as a 100 % kill rate.
    """
    tree = ast.parse(source)
    if site.operator == "delete_stmt":
        return _delete_statement(source, tree, site)
    node = _node_at(tree, site.path)
    if site.operator == "compare_swap":
        assert isinstance(node, ast.Compare)
        # Exactly one operator token: a chain `a < b < c` is one site at the
        # first token, so the mutant differs by one change and the diff stays on
        # one line.
        left = node.left
        right = node.comparators[0]
        return _replace_between(
            source, left, right, type(node.ops[0]), _COMPARE[type(node.ops[0])]
        )
    if site.operator == "arith_swap":
        assert isinstance(node, ast.BinOp)
        return _replace_between(
            source, node.left, node.right, type(node.op), _ARITH[type(node.op)]
        )
    if site.operator in ("int_plus", "int_minus"):
        assert isinstance(node, ast.Constant)
        assert isinstance(node.value, int) and not isinstance(node.value, bool), node.value
        delta = 1 if site.operator == "int_plus" else -1
        return _splice(source, _start(node), _end(node), str(node.value + delta))
    if site.operator == "bool_flip":
        if isinstance(node, ast.BoolOp):
            new_op = ast.Or if isinstance(node.op, ast.And) else ast.And
            return _replace_between(
                source, node.values[0], node.values[1], type(node.op), new_op
            )
        assert isinstance(node, ast.UnaryOp)
        operand = node.operand
        # The gap runs from the `not` to the operand's own start: `_end(node)`
        # is after the operand, so `_between` would return the empty string.
        gap = _text(source, _start(node), _start(operand))
        if "not" not in gap:
            raise ValueError(f"expected a `not` between {node} and its operand")
        # `not (a and b)` must keep the opening paren after dropping `not (`.
        replacement = "(" if "(" in gap else ""
        return _splice(source, _start(node), _start(operand), replacement)
    if site.operator == "return_none":
        assert isinstance(node, ast.Return)
        return _splice(source, _start(node), _end(node), "return None")
    if site.operator == "slice_bound":
        assert isinstance(node, ast.Slice)
        for bound in (node.lower, node.upper):
            if isinstance(bound, ast.Constant) and isinstance(bound.value, int):
                return _splice(source, _start(bound), _end(bound), str(bound.value + 1))
        raise ValueError("no integer slice bound to mutate")
    raise ValueError(f"unknown operator {site.operator!r}")


def _delete_statement(source: str, tree: ast.AST, site: Site) -> str:
    """Delete one statement, keeping the file parseable.

    Whole-line statements are deleted by line. A statement that shares its line
    with an `if`/`for` header cannot be, so its span becomes `...` — still a
    statement, still no behaviour. A statement alone in its block becomes `pass`,
    and a decorated definition is deleted from its first decorator, otherwise the
    decorator is left dangling on the following line.
    """
    statement = _statement_at(tree, site)
    assert isinstance(statement, ast.stmt)
    first = statement.lineno
    for decorator in getattr(statement, "decorator_list", []) or []:
        first = min(first, decorator.lineno)
    last = statement.end_lineno or statement.lineno
    if _line_prefix(source, statement.lineno, statement.col_offset).strip():
        return _splice(source, _start(statement), _end(statement), "...")
    if _is_alone(tree, site):
        return _splice(source, _start(statement), _end(statement), "pass")
    return _drop_lines(source, first, last)


def _statement_at(tree: ast.AST, site: Site) -> ast.stmt:
    """The statement a delete site addresses.

    `Site.path` for a delete is `(*owner_path, index)` where the index is the
    position in the owner's *statement list*, not among all of the owner's
    children, so it cannot be walked with `_node_at` — decorators, arguments and
    defaults would shift it.
    """
    wanted, field, index = site.path[:-1], site.field, site.path[-1]
    for block_path, block_field, statements in _blocks(tree, ()):
        if block_path == wanted and block_field == field and index < len(statements):
            statement = statements[index]
            assert isinstance(statement, ast.stmt)
            return statement
    raise ValueError(f"no statement for {site.key} in {field} of {wanted}")


def _is_alone(tree: ast.AST, site: Site) -> bool:
    """Is the statement at `site` the only one in its block?"""
    wanted, field, index = site.path[:-1], site.field, site.path[-1]
    for block_path, block_field, statements in _blocks(tree, ()):
        if block_path == wanted and block_field == field:
            return len(statements) == 1 and index < len(statements)
    return False


def mutate(source: str, site: Site) -> str:
    """Mutated source for one site, validated by re-parsing and by diff shape."""
    mutated = apply_site(source, site)
    ast.parse(mutated)  # a mutant that does not parse is a mutator bug
    if mutated.strip() == source.strip():
        raise ValueError(f"mutation at {site.key} produced no change")
    first, old_span, new_span = _changed_span(source, mutated)
    limit = 200 if site.operator == "delete_stmt" else 8
    if old_span > limit or new_span > limit:
        raise ValueError(
            f"mutation at {site.key} rewrote {old_span} lines into {new_span}; "
            f"a site edit must stay under {limit}"
        )
    if abs(first - site.line) > 3:
        raise ValueError(
            f"mutation at {site.key} changed line {first}, not the site's line"
        )
    return mutated


def sites_by_operator(source: str) -> dict[str, int]:
    counts: dict[str, int] = {name: 0 for name in OPERATORS}
    for site in enumerate_sites(source):
        counts[site.operator] = counts.get(site.operator, 0) + 1
    return counts
