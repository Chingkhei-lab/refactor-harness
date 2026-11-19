"""An AST mutator small enough to explain, one change per mutant.

Mutation testing answers a different question from a test run: not "do the tests
pass" but "would the tests notice if the code were wrong". The mutator is written
here rather than taken from a library for one reason — every operator has to be
explainable, and a survivor has to be attributable to a specific edit at a
specific line.

Seven operators, each applied to ONE site:

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
import copy
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


def apply_site(source: str, site: Site) -> str:
    """Return the mutated source. Exactly one change; the AST must stay valid."""
    tree = ast.parse(source)
    if site.operator == "delete_stmt":
        if not _delete_from_block(tree, site.path, site.field):
            raise ValueError(f"cannot delete statement at {site.path}")
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    node = _node_at(tree, site.path)
    if site.operator == "compare_swap":
        assert isinstance(node, ast.Compare)
        node.ops = [_COMPARE[type(op)]() for op in node.ops]
    elif site.operator == "arith_swap":
        assert isinstance(node, ast.BinOp)
        node.op = _ARITH[type(node.op)]()
    elif site.operator in ("int_plus", "int_minus"):
        assert isinstance(node, ast.Constant)
        value = node.value
        assert isinstance(value, int) and not isinstance(value, bool), value
        delta = 1 if site.operator == "int_plus" else -1
        node.value = value + delta
    elif site.operator == "bool_flip":
        if isinstance(node, ast.BoolOp):
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        else:
            assert isinstance(node, ast.UnaryOp)
            _replace_child(tree, site.path, node.operand)
    elif site.operator == "return_none":
        assert isinstance(node, ast.Return)
        node.value = ast.Constant(value=None)
    elif site.operator == "slice_bound":
        assert isinstance(node, ast.Slice)
        for name in ("lower", "upper"):
            bound = getattr(node, name)
            if isinstance(bound, ast.Constant) and isinstance(bound.value, int):
                setattr(node, name, ast.Constant(value=bound.value + 1))
                break
    else:
        raise ValueError(f"unknown operator {site.operator!r}")
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _replace_child(tree: ast.AST, path: tuple[int, ...], replacement: ast.AST) -> None:
    """Replace the node at `path` with `replacement` inside its parent."""
    parent = _node_at(tree, path[:-1])
    field_name, index = _field_of(parent, path[-1])
    if index is None:
        setattr(parent, field_name, replacement)
    else:
        getattr(parent, field_name)[index] = replacement


def _field_of(parent: ast.AST, child_index: int) -> tuple[str, int | None]:
    """Which field/index of `parent` holds the child at `child_index`."""
    position = 0
    for field_name, value in ast.iter_fields(parent):
        if isinstance(value, ast.AST):
            if position == child_index:
                return field_name, None
            position += 1
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, ast.AST):
                    if position == child_index:
                        return field_name, index
                    position += 1
    raise IndexError(child_index)


def mutate(source: str, site: Site) -> str:
    """Mutated source for one site, validated by re-parsing."""
    mutated = apply_site(copy.deepcopy(source), site)
    ast.parse(mutated)  # a mutant that does not parse is a mutator bug
    if mutated.strip() == source.strip():
        raise ValueError(f"mutation at {site.key} produced no change")
    return mutated


def sites_by_operator(source: str) -> dict[str, int]:
    counts: dict[str, int] = {name: 0 for name in OPERATORS}
    for site in enumerate_sites(source):
        counts[site.operator] = counts.get(site.operator, 0) + 1
    return counts
