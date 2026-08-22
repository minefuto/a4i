"""Work out what a POST body would change, given the current MO tree.

The APIC has no server-side dry run, so ``post --dry-run`` fetches the subtree
the body targets and compares it here. Nothing in this module performs I/O: it
takes the parsed body and the ``imdata`` of a GET, and returns the changes.

The comparison runs one way on purpose. A POST leaves every MO and every
attribute the body does not mention alone, so nothing the body is silent about
can change. Comparing both ways -- which is what comparing a whole fabric
against its intended configuration needs -- is :mod:`a4i.diff`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from a4i.mo import META, ROOT, WRAPPER, Change, Tree, child_dn, split_mo, text

_CREATED_CONFLICT = 'status="created" but the MO already exists; the POST will fail'
_MODIFIED_CONFLICT = 'status="modified" but the MO does not exist; the POST will fail'
_UNIDENTIFIED = "the body leaves out a property its RN is built from; the POST will fail"
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


@dataclass(frozen=True)
class Subtree:
    """One comparison a dry run is made of: a body, and the DN it stands at.

    ``identified`` is false when the DN had to be made up, because the body
    leaves out a property its RN is built from. There is nothing to fetch for
    one of those -- no such MO can be on the fabric under that DN -- so it is
    compared against nothing and :func:`compare` reports the warning.
    """

    mo: Any
    dn: str
    identified: bool = True


def subtrees(target: str, kind: str, mo: Any) -> list[Subtree]:
    """Return the comparisons a dry run of ``mo`` is made of.

    One, normally: the body's root, at the DN it names. A ``polUni`` is taken
    apart into its children instead. The DN it names is uni, and fetching uni
    whole with ``rsp-subtree=full`` is the one request a large fabric times out
    on -- the reason a fabric-wide comparison splits at the top level as well.
    The wrapper carries no configuration of its own to compare, so nothing is
    lost by never standing at it.

    Raises ``ValueError`` if the root names no DN at all.
    """

    parsed = split_mo(mo)
    if parsed is None:
        return []
    class_name, body = parsed
    if class_name != WRAPPER:
        dn = root_dn(target, kind, mo)
        if dn is None:
            raise ValueError(_NO_TARGET_DN)
        return [Subtree(mo, dn)]
    found: list[Subtree] = []
    for child in body.get("children") or []:
        child_parsed = split_mo(child)
        if child_parsed is None:
            continue
        child_class, child_body = child_parsed
        dn_of_child, identified = child_dn(ROOT, child_class, child_body)
        found.append(Subtree(child, dn_of_child, identified))
    return found


def compare(mo: Any, imdata: Any, dn: str, *, identified: bool = True) -> list[Change]:
    """Return the changes posting ``mo`` would cause, given the GET of ``dn``.

    ``imdata`` is the response to ``dn`` queried with ``rsp-subtree=full``; an
    empty one means the MO does not exist yet and everything is new.

    ``identified`` is :attr:`Subtree.identified`, false for a root whose DN had
    to be made up. A child gets the same treatment from the walk below; a root
    has nobody above it to work it out, so it is passed in.
    """

    parsed = split_mo(mo)
    if parsed is None:
        return []
    class_name, body = parsed
    tree = Tree(imdata)
    if len(tree.roots) == 1:
        # Prefer the DN the APIC echoed back, so the body and the current tree
        # are keyed identically even if the target was typed differently.
        dn = tree.roots[0].dn
    changes: list[Change] = []
    _visit(class_name, body, dn, tree, changes, identified=identified)
    return changes


# -- walking the body ------------------------------------------------------


def _visit(
    class_name: str,
    body: dict[str, Any],
    dn: str,
    tree: Tree,
    changes: list[Change],
    *,
    identified: bool = True,
) -> None:
    attributes = body.get("attributes") or {}
    status = _status(attributes)
    current = tree.index.get(dn)
    if not identified:
        # Before the deleted branch: a body the APIC cannot work an RN out of is
        # refused whichever way it is meant.
        changes.append(Change("warning", class_name, dn, message=_UNIDENTIFIED))
    if "deleted" in status:
        # Deleting an MO that is not there changes nothing, and the APIC accepts
        # it without complaint.
        if current is not None:
            changes.append(Change("deleted", class_name, dn, child_count=tree.descendant_count(dn)))
        # The subtree goes with the MO, so the body's children add nothing.
        return
    if current is None:
        if "modified" in status and "created" not in status:
            changes.append(Change("warning", class_name, dn, message=_MODIFIED_CONFLICT))
        changes.append(Change("created", class_name, dn, attributes=_added(attributes)))
    else:
        if "created" in status and "modified" not in status:
            changes.append(Change("warning", class_name, dn, message=_CREATED_CONFLICT))
        changed = _compare_attributes(attributes, current.attributes)
        if changed:
            changes.append(Change("modified", class_name, dn, attributes=changed))
    for child in body.get("children") or []:
        parsed = split_mo(child)
        if parsed is None:
            continue
        child_class, child_body = parsed
        # A child of an unknown class, or one written without the properties its
        # RN needs, gets a stand-in; either way it keys against the tree the same.
        dn_of_child, identified = child_dn(dn, child_class, child_body)
        _visit(child_class, child_body, dn_of_child, tree, changes, identified=identified)


def _added(attributes: dict[str, Any]) -> dict[str, tuple[str | None, str | None]]:
    return {key: (None, text(value)) for key, value in attributes.items() if key not in META}


def _compare_attributes(
    attributes: dict[str, Any], current: dict[str, Any]
) -> dict[str, tuple[str | None, str | None]]:
    """Diff the attributes the body sets against the ones the MO has now.

    Only the keys the body carries are considered: a POST leaves every other
    attribute alone, so they cannot change.
    """

    changed: dict[str, tuple[str | None, str | None]] = {}
    for key, value in attributes.items():
        if key in META:
            continue
        after = text(value)
        raw = current.get(key)
        before = None if raw is None else text(raw)
        if before != after:
            changed[key] = (before, after)
    return changed


def _status(attributes: dict[str, Any]) -> frozenset[str]:
    """Return the status tokens, e.g. "created,modified" -> {created, modified}."""

    raw = attributes.get("status")
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())
