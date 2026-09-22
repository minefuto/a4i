"""Work out what a POST body would change, given the fabric as it stands.

The APIC has no server-side dry run, so ``post --dry-run`` compares the body
against the fabric here. Nothing in this module performs I/O: :func:`check`
takes the body and the fabric it would land on, both already in hand, and
returns the changes.

Where that fabric comes from is the caller's: ``a4i fetch`` leaves one in the
daemon and :meth:`a4i.Client.dry_run` reads the subtrees :func:`roots` names.
Either is one body read down from uni, so this cannot tell them apart -- and
nothing here has to.

Both sides are read by :func:`a4i.merge.read`, which is what :mod:`a4i.diff`
reads its two sides with as well. So an MO the body names and an MO the APIC
returned key alike, and a fabric one of the three commands cannot read is a
fabric none of them can.

The comparison runs one way on purpose. A POST leaves every MO and every
attribute the body does not mention alone, so nothing the body is silent about
can change. Comparing both ways -- which is what comparing a whole fabric
against its intended configuration needs -- is :mod:`a4i.diff`.
"""

from __future__ import annotations

from typing import Any

from a4i import merge
from a4i.merge import Intended
from a4i.mo import META, WRAPPER, Change, parent_dn, split_mo
from a4i.query import Kind
from a4i.validate import read_body

_CREATED_CONFLICT = 'status="created" but the MO already exists; the POST will fail'
_MODIFIED_CONFLICT = 'status="modified" but the MO does not exist; the POST will fail'
_NO_TARGET_DN = (
    "cannot determine the target DN for a dry run "
    '(post to an mo target, or give a "dn" attribute in the body)'
)


def root_dn(target: str, kind: str, mo: Any) -> str | None:
    """Return the absolute DN the body's root MO refers to, or None.

    The ``dn`` attribute wins over the target: posting to uni with the DN
    written in the body is a common way to write ACI configuration. Failing
    that, only an MO target names one -- a class target says what the body is,
    not where it goes.
    """

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


def rooted(target: str, kind: str, body: Any) -> list[Any]:
    """Return ``body``'s root MOs, each naming the DN it stands at.

    A body is read down from uni, so a root that does not say where it sits is
    placed by its class's RN format as a child of uni. That is right for a
    merged configuration and wrong for a POST to one DN, where the target is
    what says where the MO sits. Writing the target in as the root's ``dn`` is
    the whole of the difference: what comes out is a body that says where it
    goes, which is what :func:`a4i.merge.read` already knows how to place.

    A ``polUni`` is left as it is. It stands at uni by definition, carries no
    configuration of its own, and its children are read as uni's own. So is
    anything that is not an MO at all, so that the validation in
    :func:`a4i.merge.read` is the one that says so.

    Raises ``ValueError`` if a root names no DN and the target gives none.
    """

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


def roots(index: dict[str, Any]) -> list[str]:
    """Return the DNs of the MOs in ``index`` that nothing in it sits above.

    These are the subtrees to fetch: one request each, covering everything the
    body has to say. A merged body gives uni's own children; a body posted at
    one DN gives that DN. Where the body names both a tenant and a BD inside it,
    only the tenant comes back -- fetching it covers the BD, and fetching both
    would ask the APIC for the BD twice.
    """

    return [dn for dn in index if parent_dn(dn) not in index]


def check(target: str, body: str | Any, *, kind: Kind, fabric: Any) -> list[Change]:
    """Return the changes posting ``body`` at ``target`` would cause on ``fabric``.

    ``kind`` says what ``target`` is, as on :meth:`a4i.Client.post`. ``fabric``
    is what the POST would land on -- everything under uni as ``a4i fetch``
    read it, or the subtrees :func:`roots` names, which is what
    :meth:`a4i.Client.dry_run` hands over. It is keyword-only for the reason
    :func:`a4i.diff.compare` gives: a comparison is worth only as much as the
    reader knows of where its other side came from.

    Nothing is sent. An empty list means the POST would change nothing at all.

    The fabric side is read with ``loose``, since a body posted below uni names
    a DN whose ancestors carry no configuration of their own to have come back.
    Both sides go through :func:`a4i.merge.read`, the one :meth:`a4i.Client.fetch`
    and :func:`a4i.diff.compare` read theirs with, so the body is refused
    exactly as :func:`a4i.merge.merge` would refuse it, and a fabric this
    cannot read is one none of the three can.
    """

    _, parsed = read_body(body)
    intended = merge.read(rooted(target, kind, parsed))
    return compare(intended, merge.read(fabric, loose=True))


def compare(intended: Intended, current: Intended) -> list[Change]:
    """Return the changes posting ``intended`` would cause, given ``current``.

    ``intended`` is the body read down from uni, ``current`` the fabric it
    would land on -- :func:`check` reads both. An MO the fabric does not carry
    is one the POST creates; an MO it carries is compared on the attributes the body
    sets and no others, because a POST leaves the rest alone.

    The report comes out in the order the body was read, which for a merged body
    is RN order and for a hand-written one is the order it was written. Nothing
    is sorted: only one side is walked, so the body's own order is the report's.
    """

    changes: list[Change] = []
    deleted: set[str] = set()
    for dn, node in intended.index.items():
        if _under(dn, deleted):
            # The subtree goes with the MO the body deletes, so what the body
            # says about anything inside it adds nothing.
            continue
        attributes = node.attributes
        status = _status(attributes)
        existing = current.index.get(dn)
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
            changes.append(Change("created", node.class_name, dn, attributes=_added(attributes)))
            continue
        if "created" in status and "modified" not in status:
            changes.append(Change("warning", node.class_name, dn, message=_CREATED_CONFLICT))
        changed = _compare_attributes(attributes, existing.attributes)
        if changed:
            changes.append(Change("modified", node.class_name, dn, attributes=changed))
    return changes


def _under(dn: str, deleted: set[str]) -> bool:
    """True when any MO above ``dn`` is one the body deletes.

    Every ancestor is walked rather than the parent alone: the index is keyed by
    DN and read in the order the body gave, which is not a promise that a parent
    was seen before the MOs under it.
    """

    parent = parent_dn(dn)
    while parent is not None:
        if parent in deleted:
            return True
        parent = parent_dn(parent)
    return False


def _added(attributes: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
    return {key: (None, value) for key, value in attributes.items() if key not in META}


def _compare_attributes(
    attributes: dict[str, str], current: dict[str, str]
) -> dict[str, tuple[str | None, str | None]]:
    """Diff the attributes the body sets against the ones the MO has now.

    Only the keys the body carries are considered: a POST leaves every other
    attribute alone, so they cannot change.
    """

    changed: dict[str, tuple[str | None, str | None]] = {}
    for key, value in attributes.items():
        if key in META:
            continue
        before = current.get(key)
        if before != value:
            changed[key] = (before, value)
    return changed


def _status(attributes: dict[str, str]) -> frozenset[str]:
    """Return the status tokens, e.g. "created,modified" -> {created, modified}."""

    raw = attributes.get("status")
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())
