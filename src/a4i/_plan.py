# The guarantee is one-way, and that is the point: a change the comparison misses leaves
# the fabric as it is, to be reported again next run, where a body wide enough to be
# safe against a missed comparison would be the whole configuration.

from __future__ import annotations

from typing import Any

from a4i import _dry_run as dry_run
from a4i import _merge as merge
from a4i._merge import Intended, Mo
from a4i._mo import ROOT, Change, parent_dn
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


def create(config: str | Any, *, fabric: Any) -> dict[str, Any]:
    """Return ``config`` narrowed to the MOs posting it at uni would change.

    ``config`` is one ACI body, as :func:`a4i.diff` takes one: several
    configurations are folded into it beforehand with :func:`a4i.merge`,
    which this runs it through in any case -- merging is idempotent, so a body
    that has been through it already comes out unchanged.

    ``fabric`` is what the POST would land on, as :meth:`a4i.Client.fetch`
    returns it, and is keyword-only for the reason :func:`a4i.diff`
    gives. The comparison itself is :func:`a4i.dry_run`, the one
    ``post --dry-run`` reports, run over the merged body at uni.

    Posting the result at uni does what that comparison found and touches
    nothing else, so an MO the configuration already agrees with is never handed
    back to the APIC to be written again. Every MO it can create says
    ``status="created"``; the rest assert what the fabric already had.

    Raises ``ValueError`` if the dry run warns that the POST would fail -- the
    body written for a warned MO would be a body nobody read.
    """

    _, parsed = read_body(config)
    intended = merge.read(parsed, loose=True)
    if not intended.index:
        raise ValueError(merge.EMPTY)
    changes = dry_run.compare(intended, merge.read(fabric, loose=True))
    warnings = sum(1 for change in changes if change.kind == "warning")
    if warnings:
        raise ValueError(_WARNED.format(count=plural(warnings, "warning")))
    return merge.write(_narrow(intended, changes))


# Every MO carries the status the change says it is, and the status the input wrote is
# not carried over: that said what the configuration meant in general, where this
# asserts what one read of one fabric found. Where the assertion is wrong the APIC
# refuses the POST, which is the failure this is meant to have.
def _narrow(intended: Intended, changes: list[Change]) -> Intended:
    narrowed = Intended()
    for change in changes:
        node = intended.index[change.dn]
        attributes = {"status": change.kind}
        for key, (_, after) in change.attributes.items():
            if after is not None:
                attributes[key] = after
        narrowed.index[change.dn] = Mo(node.class_name, change.dn, attributes)
    for dn in list(narrowed.index):
        parent = parent_dn(dn)
        while parent is not None and parent != ROOT and parent not in narrowed.index:
            node = intended.index[parent]
            attributes = {"status": _CONTAINER_STATUS}
            narrowed.index[parent] = Mo(node.class_name, parent, attributes)
            parent = parent_dn(parent)
    return narrowed
