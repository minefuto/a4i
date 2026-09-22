"""Narrow a merged body down to the MOs a POST of it would actually change.

A merged configuration describes what the fabric is meant to hold, all of it,
and posting the whole of it hands the APIC MOs that already read exactly that
way. The APIC touches every one of them. This module takes the same comparison
``post --dry-run`` reports and writes it back out as a body, so that what is
posted is the report and nothing besides.

Nothing here performs I/O. :func:`create` takes a configuration and the fabric
as it was fetched -- :meth:`a4i.Client.fetch` reads that, and the daemon holds
it between commands -- runs the dry run over the two, and returns both the
report and the body to post.

The guarantee is one-way, and that is the point. If the comparison misses a
change, the MO is left out and the fabric keeps what it has -- a configuration
not yet applied, which the next run reports again. Nothing outside the report
is ever written. A body wide enough to be safe against a missed comparison
would have to be the whole configuration, which is the blast radius this exists
to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from a4i import dry_run, merge
from a4i.mo import ROOT, WRAPPER, Change, child_dn, split_mo, tail_rn
from a4i.output import plural
from a4i.validate import read_body

# What a container is: an MO no change names, standing in the output only
# because something under it changed. It is on the fabric already -- a
# comparison that did not report it as created is a comparison that found it --
# so it says as much, and the APIC refuses the POST outright if it is not.
# Without this it would read as create-or-modify, and a comparison gone wrong
# would quietly grow an empty MO where it thought one stood.
_CONTAINER_STATUS = "modified"

_WARNED = (
    "refusing to write a plan: the dry run reported {count}, "
    "so this POST would fail as written. Fix the body and try again"
)
_LOST = (
    "refusing to write a plan: {count} the dry run reported "
    "cannot be placed in the merged body it was made from"
)


@dataclass(frozen=True)
class Plan:
    """A POST narrowed to what it changes: the body to send, and what it means.

    ``changes`` is the report ``post --dry-run`` prints, and ``body`` is those
    same changes as one body. They come from a single read of the fabric, so
    what a reader is shown and what a POST would send cannot be answers to two
    different questions.
    """

    body: dict[str, Any]
    changes: list[Change] = field(default_factory=list)

    @property
    def containers(self) -> int:
        """How many MOs the body carries that no change names.

        The report has no line for these: they are in the body only to nest what
        does change (see :data:`_CONTAINER_STATUS`). A reader comparing the two
        has to be told how many, or the body reads as a comparison that found
        more than it said.
        """

        return count(self.body) - len(self.changes)


def create(config: str | Any, *, fabric: Any) -> Plan:
    """Return ``config`` narrowed to the MOs posting it at uni would change.

    ``config`` is one ACI body, as :func:`a4i.diff.compare` takes one: several
    configurations are folded into it beforehand with :func:`a4i.merge.merge`,
    which this runs it through in any case -- the placement rules, the refusals
    and the output shape are the merged body's, and merging is idempotent, so a
    body that has been through it already comes out unchanged.

    ``fabric`` is what the POST would land on, as :meth:`a4i.Client.fetch`
    returns it, and is keyword-only for the reason :func:`a4i.diff.compare`
    gives. The comparison itself is :func:`a4i.dry_run.check`, the one
    ``post --dry-run`` reports, run over the merged body at uni. Only what the
    configuration names is looked at, the rest of the fabric being nothing a
    POST of it could touch.

    The result carries the changes ``post --dry-run`` reports and the body those
    changes make between them. Posting that body at uni does what the changes
    say and touches nothing else, so an MO the configuration already agrees with
    is never handed back to the APIC to be written again.

    Raises ``ValueError`` if the dry run warns that the POST would fail -- the
    body written for a warned MO would be a body nobody read. See :func:`body`.
    """

    _, parsed = read_body(config)
    merged = merge.merge(parsed)
    changes = dry_run.check(ROOT, merged, kind="mo", fabric=fabric)
    return Plan(body(merged, changes), changes)


def body(merged: dict[str, Any], changes: list[Change]) -> dict[str, Any]:
    """Return the body posting only ``changes`` takes, wrapped for uni.

    ``merged`` is a :func:`a4i.merge.merge` result and ``changes`` is what
    :func:`a4i.dry_run.compare` made of it. The output is shaped exactly as a
    merged body is -- a ``polUni`` of MOs nested under the MO each hangs off,
    each naming itself by its ``rn`` -- so the two can be read side by side.
    What differs is what is in it: the MOs the changes name, the MOs they hang
    under, and nothing else.

    Every MO carries the ``status`` the change says it is: ``created`` for one
    the fabric does not have, ``modified`` for one it has, ``deleted`` for one
    the body asks to remove. The status the input wrote is not carried over. It
    said what the configuration meant in general; this body is about one POST
    against one fabric that was just read, and the status here asserts what that
    read found. Where the assertion is wrong the APIC refuses the POST, which is
    the failure this is meant to have.

    A ``modified`` MO carries only the attributes that change. A POST leaves
    every attribute it does not mention alone, so the rest would be the fabric's
    own values handed back to it.

    Raises ``ValueError`` if any change is a warning -- the report says the POST
    will fail, and a body that quietly succeeded instead would be doing
    something nobody read.
    """

    warnings = sum(1 for change in changes if change.kind == "warning")
    if warnings:
        raise ValueError(_WARNED.format(count=plural(warnings, "warning")))
    wanted = {change.dn: change for change in changes if change.kind != "warning"}
    children = _children(ROOT, merged.get(WRAPPER) or {}, wanted)
    if wanted:
        raise ValueError(_LOST.format(count=plural(len(wanted), "MO")))
    return {WRAPPER: {"attributes": {"dn": ROOT}, "children": children}}


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


def _children(dn: str, body_of: dict[str, Any], wanted: dict[str, Change]) -> list[dict[str, Any]]:
    """Return the MOs under ``dn`` that belong in the plan, in the merged order.

    ``wanted`` is emptied as it goes, so that a change left in it at the end is
    one this walk never reached -- a change that would otherwise be posted by
    nobody while the report said it would be.
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
            # The subtree goes with the MO. Anything under it the comparison
            # would have reported is a change the comparison never made.
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
