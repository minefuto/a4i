"""Narrow a merged body down to the MOs a POST of it would actually change.

:func:`create` runs the comparison ``post --dry-run`` reports and writes it back
out as a body, so that what is posted is what that comparison found and nothing
besides. Nothing here performs I/O, and nothing here reports: the report is
``post --dry-run``'s, run against the same fetched fabric.

The guarantee is one-way, and that is the point: a change the comparison misses
leaves the fabric as it is, to be reported again next run, where a body wide
enough to be safe against a missed comparison would be the whole configuration.
"""

from __future__ import annotations

from typing import Any

from a4i import _dry_run as dry_run
from a4i import _merge as merge
from a4i._mo import ROOT, WRAPPER, Change, child_dn, split_mo, tail_rn
from a4i._output import plural
from a4i._validate import read_body

# A container -- an MO no change names, in the body only to nest what changed --
# is on the fabric already, so it says as much and the APIC refuses the POST
# outright if it is not. The APIC's own default would be create-or-modify, where
# a comparison gone wrong would quietly grow an empty MO instead.
_CONTAINER_STATUS = "modified"

_WARNED = (
    "refusing to write a plan: the dry run reported {count}, "
    "so this POST would fail as written. Fix the body and try again; "
    "post --dry-run names the MOs"
)
_LOST = (
    "refusing to write a plan: {count} the dry run reported "
    "cannot be placed in the merged body it was made from"
)


def create(config: str | Any, *, fabric: Any) -> dict[str, Any]:
    """Return ``config`` narrowed to the MOs posting it at uni would change.

    ``config`` is one ACI body, as :func:`a4i._diff.compare` takes one: several
    configurations are folded into it beforehand with :func:`a4i._merge.merge`,
    which this runs it through in any case -- merging is idempotent, so a body
    that has been through it already comes out unchanged.

    ``fabric`` is what the POST would land on, as :meth:`a4i.Client.fetch`
    returns it, and is keyword-only for the reason :func:`a4i._diff.compare`
    gives. The comparison itself is :func:`a4i._dry_run.check`, the one
    ``post --dry-run`` reports, run over the merged body at uni.

    Posting the result at uni does what that comparison found and touches
    nothing else, so an MO the configuration already agrees with is never handed
    back to the APIC to be written again. Every MO it can create says
    ``status="created"``; the rest assert what the fabric already had.

    Raises ``ValueError`` if the dry run warns that the POST would fail -- the
    body written for a warned MO would be a body nobody read.
    """

    _, parsed = read_body(config)
    merged = merge.merge(parsed)
    return _body(merged, dry_run.check(ROOT, merged, kind="mo", fabric=fabric))


def count(plan: dict[str, Any]) -> int:
    """Return how many MOs a plan carries, the wrapper aside."""

    def below(children: Any) -> int:
        total = 0
        for child in children if isinstance(children, list) else []:
            parsed = split_mo(child)
            if parsed is None:
                continue
            total += 1 + below(parsed[1].get("children"))
        return total

    return below((plan.get(WRAPPER) or {}).get("children"))


# -- walking the merged body -----------------------------------------------


def _body(merged: dict[str, Any], changes: list[Change]) -> dict[str, Any]:
    """Return the body posting only ``changes`` takes, wrapped for uni.

    Shaped exactly as the merged body it was made from, so the two can be read
    side by side; what differs is what is in it.

    Every MO carries the ``status`` the change says it is, and the status the
    input wrote is not carried over: that said what the configuration meant in
    general, where this asserts what one read of one fabric found. Where the
    assertion is wrong the APIC refuses the POST, which is the failure this is
    meant to have.
    """

    warnings = sum(1 for change in changes if change.kind == "warning")
    if warnings:
        raise ValueError(_WARNED.format(count=plural(warnings, "warning")))
    wanted = {change.dn: change for change in changes if change.kind != "warning"}
    children = _children(ROOT, merged.get(WRAPPER) or {}, wanted)
    if wanted:
        raise ValueError(_LOST.format(count=plural(len(wanted), "MO")))
    return {WRAPPER: {"attributes": {"dn": ROOT}, "children": children}}


def _children(dn: str, body_of: dict[str, Any], wanted: dict[str, Change]) -> list[dict[str, Any]]:
    """Return the MOs under ``dn`` that belong in the plan, in the merged order.

    ``wanted`` is emptied as it goes, so that a change left in it at the end is
    one this walk never reached -- a change that would otherwise be posted by
    nobody while the comparison said it would be.
    """

    kept: list[dict[str, Any]] = []
    for child in body_of.get("children") or []:
        parsed = split_mo(child)
        if parsed is None:
            continue
        class_name, child_body = parsed
        dn_of_child, _ = child_dn(dn, class_name, child_body)
        change = wanted.pop(dn_of_child, None)
        if change is not None and change.kind == "deleted":
            # The subtree goes with the MO, so nothing under it is walked.
            kept.append({class_name: _attributes(dn_of_child, change)})
            continue
        below = _children(dn_of_child, child_body, wanted)
        if change is None and not below:
            continue
        mo_body = _attributes(dn_of_child, change)
        if below:
            mo_body["children"] = below
        kept.append({class_name: mo_body})
    return kept


def _attributes(dn: str, change: Change | None) -> dict[str, Any]:
    """Return the body of one MO in the plan: its RN, its status, what changes."""

    attributes = {"rn": tail_rn(dn)}
    if change is None:
        attributes["status"] = _CONTAINER_STATUS
        return {"attributes": attributes}
    attributes["status"] = change.kind
    for key, (_, after) in change.attributes.items():
        if after is not None:
            attributes[key] = after
    return {"attributes": attributes}
