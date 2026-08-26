# ruff: noqa: CPY001
"""Data-driven filter specs for `WorkflowQuery`.

Lets a filter be described as data (a dict, typically loaded from YAML/JSON)
instead of Python code, so it can be supplied dynamically -- e.g. from a CLI
flag or a config file -- while compiling down to the exact same
`pyarrow.dataset` expressions `WorkflowQuery`'s Python API produces. There is
no `eval()`/`exec()` involved: only a fixed, known set of operators is
recognized, so the spec is safe to load from untrusted or user-supplied files.

Spec shape (conditions can be nested arbitrarily deep):
    {"all": [<condition-or-group>, ...]}          # AND
    {"any": [<condition-or-group>, ...]}          # OR
    {"not": <condition-or-group>}                 # NOT
    {"column": "...", "op": "eq", "value": ...}   # leaf condition

Supported ops: eq, ne, gt, ge, lt, le, in, between, is_null, is_not_null,
starts_with, contains, list_contains.

Example:
    ```yaml
    all:
      - column: workflow_conclusion
        op: eq
        value: failure
      - any:
          - column: job_conclusion
            op: eq
            value: cancelled
          - column: job_duration_sec
            op: gt
            value: 600
    ```
"""

from __future__ import annotations

import operator
import re
from typing import TYPE_CHECKING, Any

import pyarrow.compute as pc
import pyarrow.dataset as ds

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

# Separator used to join list-column elements before a list_contains regex
# search; arbitrary but must be unlikely to appear inside a real element.
_LIST_JOIN_SEP = "\x1f"


def _list_contains(field: ds.Expression, value: Any) -> ds.Expression:  # noqa: ANN401
    """Build an expression true when list column `field` contains `value` exactly.

    There is no pyarrow.dataset kernel for per-row list membership, so this
    joins each row's list into one string and regex-matches `value` between
    separators (or string start/end), which still compiles to a single
    pushdown-able Expression.
    """
    joined = pc.binary_join(field, _LIST_JOIN_SEP)
    pattern = rf"(^|{_LIST_JOIN_SEP}){re.escape(str(value))}({_LIST_JOIN_SEP}|$)"
    return pc.match_substring_regex(joined, pattern)


_OPS: dict[str, Callable[[ds.Expression, Any], ds.Expression]] = {
    "eq": lambda field, value: field == value,
    "ne": lambda field, value: field != value,
    "gt": lambda field, value: field > value,
    "ge": lambda field, value: field >= value,
    "lt": lambda field, value: field < value,
    "le": lambda field, value: field <= value,
    "in": lambda field, value: field.isin(list(value)),
    "between": lambda field, value: (field >= value[0]) & (field <= value[1]),
    "is_null": lambda field, _value: ~field.is_valid(),
    "is_not_null": lambda field, _value: field.is_valid(),
    # Partial string matches, e.g. for job names only known by their prefix.
    "starts_with": pc.starts_with,
    "contains": pc.match_substring,
    # Exact-element match for list columns, e.g. job_runner_labels.
    "list_contains": _list_contains,
}


def build_expression(spec: Mapping[str, Any]) -> ds.Expression:
    """Recursively compile a declarative filter spec into a pyarrow.dataset expression."""
    if "all" in spec:
        return _combine(spec["all"], operator.and_, "all")
    if "any" in spec:
        return _combine(spec["any"], operator.or_, "any")
    if "not" in spec:
        return ~build_expression(spec["not"])

    return _build_condition(spec)


def _build_condition(spec: Mapping[str, Any]) -> ds.Expression:
    try:
        column = spec["column"]
        op = spec["op"]
    except KeyError as exc:
        msg = f"Filter condition is missing required key: {exc}"
        raise ValueError(msg) from exc

    try:
        apply_op = _OPS[op]
    except KeyError:
        msg = f"Unsupported filter operator {op!r}, expected one of {sorted(_OPS)}"
        raise ValueError(msg) from None

    return apply_op(ds.field(column), spec.get("value"))


def _combine(
    nodes: Iterable[Mapping[str, Any]],
    combine: Callable[[ds.Expression, ds.Expression], ds.Expression],
    keyword: str,
) -> ds.Expression:
    expr: ds.Expression | None = None
    for node in nodes:
        node_expr = build_expression(node)
        expr = node_expr if expr is None else combine(expr, node_expr)

    if expr is None:
        msg = f"'{keyword}' group must contain at least one condition"
        raise ValueError(msg)

    return expr
