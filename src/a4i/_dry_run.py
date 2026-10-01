# The APIC has no server-side dry run, so post --dry-run compares here. The comparison
# runs one way on purpose, a POST leaving alone everything the body does not mention;
# comparing both ways is a4i._diff.

from __future__ import annotations

from typing import Any

from a4i import _merge as merge
from a4i._merge import Intended
from a4i._mo import WRAPPER, Change, is_under, parent_dn, split_mo
from a4i._output import DryRunResult
from a4i._query import Kind
from a4i._validate import read_body

_CREATED_CONFLICT = 'status="created" but the MO already exists; the POST will fail'
_MODIFIED_CONFLICT = 'status="modified" but the MO does not exist; the POST will fail'
_FILLED_MISSING = "the body hangs under this MO, which does not exist; the POST will fail"
_NO_TARGET_DN = (
    "cannot determine the target DN for a dry run "
    '(post to an mo target, or give a "dn" attribute in the body)'
)


# The dn attribute wins over the target, which is a common way to write ACI
# configuration. Failing that, only an MO target names one -- a class target says what
# the body is, not where it goes.
def root_dn(target: str, kind: str, mo: Any) -> str | None:
    parsed = split_mo(mo)
    if parsed is None:
        return None
    _, body = parsed
    dn = (body.get("attributes") or {}).get("dn")
    if isinstance(dn, str) and dn.strip("/"):
        return dn.strip("/")
    if kind == "mo" and target.strip().strip("/"):
        return target.strip().strip("/")
    return None


# a4i._merge.read reads a body down from uni, so a root that does not say where it sits
# would be placed directly under uni rather than under the target. A polUni, and
# anything that is not an MO at all, is left for a4i._merge.read to judge.
def rooted(target: str, kind: str, body: Any) -> list[Any]:
    given = body if isinstance(body, list) else [body]
    found: list[Any] = []
    for root in given:
        parsed = split_mo(root)
        if parsed is None:
            found.append(root)
            continue
        class_name, mo_body = parsed
        if class_name == WRAPPER:
            found.append(root)
            continue
        dn = root_dn(target, kind, root)
        if dn is None:
            raise ValueError(_NO_TARGET_DN)
        attributes = {**(mo_body.get("attributes") or {}), "dn": dn}
        found.append({class_name: {**mo_body, "attributes": attributes}})
    return found


# The MOs the body itself stands at, each read as a subtree. What loose filled in above
# them is read on its own, see filled: its subtree is everything the body leaves alone.
def roots(index: dict[str, Any]) -> list[str]:
    return [
        dn
        for dn, node in index.items()
        if not node.filled
        and ((parent := parent_dn(dn)) not in index or parent is None or index[parent].filled)
    ]


def filled(index: dict[str, Any]) -> list[str]:
    return [dn for dn, node in index.items() if node.filled]


def check(target: str, body: str | Any, *, kind: Kind, fabric: Any) -> DryRunResult:
    """Return the changes posting ``body`` at ``target`` would cause on ``fabric``.

    ``kind`` says what ``target`` is, as on :meth:`a4i.Client.post`. ``fabric``
    is what the POST would land on -- everything under uni as ``a4i fetch`` read
    it, or the subtrees under the MOs the body stands at, which is what
    :meth:`a4i.Client.dry_run` reads. It is keyword-only for the reason
    :func:`a4i.diff` gives.

    Nothing is sent. An empty list means the POST would change nothing at all.

    Both sides are read with ``loose``, since a body posted below uni names a DN
    whose ancestors it does not describe. An ancestor filled in that way is taken
    to be on the fabric already, and a warning if it is not.
    """

    _, parsed = read_body(body)
    intended = merge.read(rooted(target, kind, parsed), loose=True)
    return DryRunResult(compare(intended, merge.read(fabric, loose=True)))


def compare(intended: Intended, current: Intended) -> list[Change]:
    changes: list[Change] = []
    deleted: set[str] = set()
    for dn, node in intended.index.items():
        if is_under(dn, deleted):
            # The subtree goes with the MO the body deletes, so what the body
            # says about anything inside it adds nothing.
            continue
        attributes = node.attributes
        status = _status(attributes)
        existing = current.index.get(dn)
        if node.filled:
            if existing is None:
                changes.append(Change("warning", node.class_name, dn, message=_FILLED_MISSING))
            continue
        if "deleted" in status:
            # Deleting an MO that is not there changes nothing, and the APIC
            # accepts it without complaint.
            if existing is not None:
                changes.append(
                    Change("deleted", node.class_name, dn, child_count=current.descendant_count(dn))
                )
            deleted.add(dn)
            continue
        if existing is None:
            if "modified" in status and "created" not in status:
                changes.append(Change("warning", node.class_name, dn, message=_MODIFIED_CONFLICT))
            changes.append(
                Change("created", node.class_name, dn, attributes=merge.added(attributes))
            )
            continue
        if "created" in status and "modified" not in status:
            changes.append(Change("warning", node.class_name, dn, message=_CREATED_CONFLICT))
        changed = merge.changed(attributes, existing.attributes)
        if changed:
            changes.append(Change("modified", node.class_name, dn, attributes=changed))
    return changes


def _status(attributes: dict[str, str]) -> frozenset[str]:
    raw = attributes.get("status")
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())
