# What a target names is never inferred: build_path is told, as kind, whether it is a
# class name or a DN, and the caller always knows which it has.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

# What a target names. These are the CLI's get/post subcommands verbatim, and
# the value the library takes as ``kind``.
Kind = Literal["class", "mo"]
KINDS: tuple[Kind, ...] = ("class", "mo")

# The names below are the ACI query parameter names verbatim, so that a
# parameter read in the APIC documentation can be typed as-is -- as a CLI option
# with dashes, or as a keyword argument with underscores.


class QueryTarget(StrEnum):
    self = "self"
    children = "children"
    subtree = "subtree"


class RspSubtree(StrEnum):
    no = "no"
    children = "children"
    full = "full"


class RspSubtreeInclude(StrEnum):
    audit_logs = "audit-logs"
    event_logs = "event-logs"
    faults = "faults"
    fault_records = "fault-records"
    health = "health"
    health_records = "health-records"
    relations = "relations"
    stats = "stats"
    tasks = "tasks"
    # Modifiers, used with a category or on their own: "faults,no-scoped".
    count = "count"
    no_scoped = "no-scoped"
    required = "required"


class RspPropInclude(StrEnum):
    all = "all"
    naming_only = "naming-only"
    config_only = "config-only"


def values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


def build_path(target: str, kind: str) -> str:
    target = target.strip().strip("/")
    if kind == "class":
        return f"/api/class/{target}.json"
    if kind == "mo":
        return f"/api/mo/{target}.json"
    raise ValueError(f"invalid kind: {kind!r} (choose from {', '.join(KINDS)})")


# params is an escape hatch for a parameter this dictionary does not know: merged last,
# unvalidated, and overriding anything named above it.
def build_get_params(
    *,
    query_target: str | None = None,
    target_subtree_class: str | Sequence[str] | None = None,
    query_target_filter: str | None = None,
    rsp_subtree: str | None = None,
    rsp_subtree_class: str | Sequence[str] | None = None,
    rsp_subtree_filter: str | None = None,
    rsp_subtree_include: str | Sequence[str] | None = None,
    rsp_prop_include: str | None = None,
    order_by: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    params: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    if (page is None) != (page_size is None):
        # ACI paginates on the pair; one without the other has no defined meaning.
        raise ValueError("page and page_size must be given together")
    # The order below is the order the parameters appear in the URL -- httpx2
    # preserves the insertion order -- so it follows the grouping the APIC
    # documentation uses: scoping filters, then response subtree filters, then
    # sorting and paging.
    mapped: dict[str, object | None] = {
        "query-target": _one_of("query_target", query_target, QueryTarget),
        "target-subtree-class": _joined(target_subtree_class),
        "query-target-filter": query_target_filter,
        "rsp-subtree": _one_of("rsp_subtree", rsp_subtree, RspSubtree),
        "rsp-subtree-class": _joined(rsp_subtree_class),
        "rsp-subtree-filter": rsp_subtree_filter,
        "rsp-subtree-include": _csv("rsp_subtree_include", rsp_subtree_include, RspSubtreeInclude),
        "rsp-prop-include": _one_of("rsp_prop_include", rsp_prop_include, RspPropInclude),
        "order-by": order_by,
        "page": _at_least("page", page, 0),
        "page-size": _at_least("page_size", page_size, 1),
    }
    # Tested against None, not truthiness: "page=0" is the first page.
    built = {key: str(value) for key, value in mapped.items() if value is not None}
    if params:
        built.update({key: str(value) for key, value in params.items()})
    return built


# -- validation ------------------------------------------------------------


def _one_of(name: str, value: str | None, enum: type[StrEnum]) -> str | None:
    if value is None:
        return None
    allowed = values(enum)
    if value not in allowed:
        raise ValueError(f"invalid {name}: {value!r} (choose from {', '.join(allowed)})")
    return value


# Class names are never held against the bundled dictionary, which may be older than the
# fabric.
def _joined(value: str | Sequence[str] | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return ",".join(value)


def _csv(name: str, value: str | Sequence[str] | None, enum: type[StrEnum]) -> str | None:
    joined = _joined(value)
    if joined is None:
        return None
    items = [item.strip() for item in joined.split(",")]
    allowed = values(enum)
    unknown = [item for item in items if item not in allowed]
    if unknown:
        raise ValueError(
            f"invalid {name} value: {', '.join(unknown)} (choose from {', '.join(allowed)})"
        )
    return ",".join(items)


def _at_least(name: str, value: int | None, minimum: int) -> int | None:
    if value is None:
        return None
    if value < minimum:
        raise ValueError(f"{name} must be {minimum} or greater, not {value}")
    return value
