"""Compare a fabric's whole configuration against the one it is meant to have.

Nothing in this module performs I/O: :func:`compare` takes the configuration and
the fabric as :meth:`a4i.Client.fetch` read it, reads each into an index with
:func:`a4i._merge.read`, and returns the differences.

It runs both ways, which is the difference from :mod:`a4i._dry_run`: a POST can
only add or change, but a fabric can carry what nobody wrote down. The intended
configuration is therefore taken to describe the whole of ``uni``, and anything
it leaves out is reported as ``extra`` unless ``exclude`` says otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from a4i._merge import Intended, read
from a4i._mo import Change, Exclusions, parent_dn, split_condition
from a4i._validate import read_body

# "status" tells the APIC what to do with an MO, so the fabric never has a value
# to hold it against. The rest of a4i._mo.META is already gone by now:
# a4i._merge.read drops it from both sides on the way in.
_INSTRUCTION = frozenset({"status"})


def compare(
    config: str | Any,
    *,
    fabric: Any,
    expand: bool = False,
    exclude: str | Sequence[str] | None = None,
) -> list[Change]:
    """Return how ``fabric`` differs from the intended configuration.

    ``config`` is one ACI body -- one MO, a list of them, or the same as JSON
    text -- describing the whole of ``uni``; several are folded into one
    beforehand by :func:`a4i._merge.merge`. ``fabric`` is what
    :meth:`a4i.Client.fetch` returns, and is keyword-only so that every call says
    the word, a comparison being worth only as much as the reader knows of where
    its other side came from. Without ``expand``, a subtree that is wholly
    missing or wholly extra is reported as its top MO alone, with the MOs below
    it counted rather than listed.

    ``exclude`` names MOs to leave out, each by its DN or by a pattern holding a
    "*", and each standing for everything under it as well; a leading "!" makes
    one an exception to the others, and a trailing ``[key=value]`` narrows one to
    the MOs whose attribute matches.

    Raises :class:`ValueError` if the configuration is not written as ACI
    expects, or if an MO does not carry the properties its RN is built from:
    such a body names no one MO, and the one it meant may well be on the fabric.
    An empty configuration is refused too, taken at face value meaning every MO
    on the fabric is extra.
    """

    excluded = _exclusions(exclude)
    _, parsed = read_body(config)
    intended = read(parsed, excluded=excluded)
    if not intended.index:
        raise ValueError(
            "the configuration is empty: it describes no MO at all, so every MO on the "
            "fabric would be reported as extra"
        )
    # Emptiness is judged before pruning, so a configuration whose every MO is
    # excluded is a comparison narrowed to nothing -- which is a report with no
    # differences in it -- and not an input that said nothing.
    actual = read(fabric)
    excluded.resolve(intended.index)
    excluded.resolve(actual.index)
    _prune(intended.index, excluded)
    _prune(actual.index, excluded)
    changes = _missing_and_modified(intended, actual, expand=expand)
    changes.extend(_extra(intended, actual, expand=expand))
    # A DN is reported at most once, so this orders the report without merging
    # anything: MOs read in tree order, and a tenant's changes stay together.
    changes.sort(key=lambda change: change.dn)
    return changes


# -- what is left out ------------------------------------------------------


def _exclusions(exclude: str | Sequence[str] | None) -> Exclusions:
    """Return what to leave out, each name read the way any other DN is read.

    A single string is one name and is never split: an ACI naming value can hold
    a comma, which is also why one name takes one condition and not several.

    Three things are refused rather than read, each of them a spelling that would
    otherwise quietly exclude nothing: an empty DN, which is what an unset shell
    variable expands to; a "**", which a reader of gitignore would write for what
    a "*" here does not do; and nothing but "!" exceptions, which excludes what
    the comparison already compares.

    The condition is cut off first, so everything above is judged on the DN
    alone. Its value is a "*" pattern as an RN is, and needs no "**" rule: an
    attribute value has no "/" to cross.
    """

    if exclude is None:
        return Exclusions()
    given = [exclude] if isinstance(exclude, str) else list(exclude)
    dns: set[str] = set()
    for name in given:
        kept = name.strip().startswith("!")
        rest, condition = split_condition(name.strip().removeprefix("!").strip())
        dn = rest.strip().strip("/")
        if not dn:
            raise ValueError("an excluded DN cannot be empty")
        if "**" in dn:
            raise ValueError(
                f'"{dn}" cannot be excluded: "**" is not supported, and "*" matches within '
                "one RN only -- name the depth, as in uni/tn-*/BD-*"
            )
        if condition is not None:
            key, value = condition
            if not key:
                raise ValueError(
                    f'"{name.strip()}" cannot be excluded: a condition names an attribute, '
                    "as in uni/tn-*/BD-*[descr=auto-*]"
                )
            dn = f"{dn}[{key}={value}]"
        dns.add(f"!{dn}" if kept else dn)
    if dns and all(dn.startswith("!") for dn in dns):
        raise ValueError(
            'a "!" name is an exception to what is excluded, so exceptions alone exclude '
            'nothing -- name what to leave out as well, as in "uni/tn-*" with "!uni/tn-mgmt"'
        )
    return Exclusions(dns)


def _prune(index: dict[str, Any], excluded: Exclusions) -> None:
    """Drop the excluded MOs from one side's index, in place.

    Both sides are pruned, excluding something being saying nothing about it at
    all. Pruning the index rather than filtering the report is what keeps
    ``child_count`` honest, being read off these same dicts.
    """

    if not excluded:
        return
    for dn in [dn for dn in index if excluded.covers(dn)]:
        del index[dn]


# -- the comparison --------------------------------------------------------


def _missing_and_modified(intended: Intended, actual: Intended, *, expand: bool) -> list[Change]:
    changes: list[Change] = []
    for dn, node in intended.index.items():
        current = actual.index.get(dn)
        if current is not None:
            attributes = _compare(node.attributes, current.attributes)
            if attributes:
                changes.append(Change("modified", node.class_name, dn, attributes=attributes))
            continue
        if not expand and _under_a_missing_parent(dn, intended, actual):
            continue
        changes.append(
            Change(
                "missing",
                node.class_name,
                dn,
                attributes=_only_intended(node.attributes),
                child_count=0 if expand else intended.descendant_count(dn),
            )
        )
    return changes


def _extra(intended: Intended, actual: Intended, *, expand: bool) -> list[Change]:
    changes: list[Change] = []
    for dn, node in actual.index.items():
        if dn in intended.index:
            continue
        if not expand and _under_an_extra_parent(dn, intended, actual):
            continue
        changes.append(
            Change(
                "extra",
                node.class_name,
                dn,
                attributes=_only_actual(node.attributes),
                child_count=0 if expand else actual.descendant_count(dn),
            )
        )
    return changes


def _under_a_missing_parent(dn: str, intended: Intended, actual: Intended) -> bool:
    """True when this MO's parent is missing too, so it goes with the parent.

    Only the parent is looked at: a missing grandparent rolls the parent up in
    turn, so the whole subtree collapses onto its top MO.
    """

    parent = parent_dn(dn)
    return parent is not None and parent in intended.index and parent not in actual.index


def _under_an_extra_parent(dn: str, intended: Intended, actual: Intended) -> bool:
    """True when this MO's parent is extra too, so it goes with the parent."""

    parent = parent_dn(dn)
    return parent is not None and parent in actual.index and parent not in intended.index


def _compare(
    intended: dict[str, str], actual: dict[str, str]
) -> dict[str, tuple[str | None, str | None]]:
    """Diff one MO's attributes both ways.

    An attribute the fabric carries and the configuration does not is reported
    with nothing on the right. The APIC returns an unset attribute as an empty
    string, which is a value the configuration does not account for either.
    """

    changed: dict[str, tuple[str | None, str | None]] = {}
    for key, value in intended.items():
        if key in _INSTRUCTION:
            continue
        before = actual.get(key)
        if before != value:
            changed[key] = (before, value)
    for key, value in actual.items():
        if key in _INSTRUCTION or key in intended:
            continue
        changed[key] = (value, None)
    # Sorted so that the report does not depend on the order the attributes were
    # written in, nor on the order the APIC returned them.
    return dict(sorted(changed.items()))


def _only_intended(attributes: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
    return {
        key: (None, value) for key, value in sorted(attributes.items()) if key not in _INSTRUCTION
    }


def _only_actual(attributes: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
    return {
        key: (value, None) for key, value in sorted(attributes.items()) if key not in _INSTRUCTION
    }
