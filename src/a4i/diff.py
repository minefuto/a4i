"""Compare a fabric's whole configuration against the one it is meant to have.

``a4i diff`` reads the intended configuration, fetches everything under ``uni``
from the APIC, and reports where the two disagree. Nothing in this module
performs I/O: it takes the parsed configuration and the ``imdata`` of those
GETs, and returns the differences.

This runs both ways, which is the whole point and the difference from
:mod:`a4i.dry_run`. A POST can only add or change, so a dry run need only look
at what the body mentions; this also has to report what the fabric carries and
the intended configuration does not -- the BD someone added by hand. So an MO
present on only one side is reported from either side (``missing`` and
``extra``), and so is an attribute.

The intended configuration is therefore taken to describe the whole of ``uni``.
Anything it leaves out is reported as ``extra``, including the tenants and
policies the APIC creates for itself. ``exclude`` narrows that: an MO named
there, and everything under it, is left out of the comparison altogether. A name
may hold a "*", which stands for any part of one RN -- ``uni/tn-test*`` -- and
:class:`a4i.mo.Exclusions` is what reads one.

One configuration is compared, not several: folding several into one is
:func:`a4i.merge.merge`, which this shares the reading of a body with but does
not call. Both read an input into :class:`a4i.merge.Intended`; only merge writes
one back out as a body.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from a4i.merge import Intended, unidentified_message
from a4i.mo import META, Change, Exclusions, Tree, parent_dn, text

# What the merged configuration carries for the sake of a POST and a comparison
# has nothing to say about: "status" tells the APIC what to do with an MO, so
# the fabric never has a value to hold it against. See a4i.merge._DROPPED.
_INSTRUCTION = frozenset({"status"})


def compare(
    config: Any,
    imdata: Any,
    *,
    expand: bool = False,
    exclude: str | Sequence[str] | None = None,
) -> list[Change]:
    """Return how ``imdata`` differs from the intended configuration.

    ``config`` is one ACI body -- one MO, or a list of them -- describing the
    whole of ``uni``; ``imdata`` is everything under ``uni``. Several
    configurations are merged into that one body beforehand by
    :func:`a4i.merge.merge`. Without ``expand``, a subtree that is wholly
    missing or wholly extra is reported as its top MO alone, with the MOs below
    it counted rather than listed.

    ``exclude`` names MOs to leave out, each by its DN or by a pattern holding a
    "*", and each standing for everything under it as well. See
    :func:`_exclusions` for what a name there means and :func:`_prune` for what
    leaving one out does.

    Raises :class:`ValueError` if an MO does not carry the properties its RN is
    built from, rather than comparing what is left: such a body names no one MO,
    and the one it meant may well be on the fabric. Reporting that one missing
    while reporting the real one extra is worse than saying what the input has to
    spell out. An empty configuration is refused for a related reason: taken at
    face value it means every MO on the fabric is extra, and what it actually
    means is almost always a path that pointed at nothing.
    """

    excluded = _exclusions(exclude)
    actual = Tree(imdata)
    intended = Intended(excluded)
    intended.absorb(config)
    if intended.unidentified:
        raise ValueError(unidentified_message(intended.unidentified))
    if not intended.index:
        raise ValueError(
            "the configuration is empty: it describes no MO at all, so every MO on the "
            "fabric would be reported as extra"
        )
    # Emptiness is judged before pruning, so a configuration whose every MO is
    # excluded is a comparison narrowed to nothing -- which is a report with no
    # differences in it -- and not an input that said nothing.
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

    A single string is one name rather than a list to split on something: an ACI
    naming value can hold a comma, so nothing here separates one name from the
    next. A leading or trailing "/" is dropped, as :func:`a4i.query.build_path`
    drops it, and what is left of nothing but those is refused: an empty DN
    names no MO, and it is what an unset shell variable expands to.

    A "*" makes the name a pattern, as :class:`a4i.mo.Exclusions` describes.
    "**" is refused rather than read as two of them: matching across RNs is the
    one thing a pattern here does not do, and a "**" written for what gitignore
    means by it would otherwise match nothing and quietly exclude nothing.
    """

    if exclude is None:
        return Exclusions()
    given = [exclude] if isinstance(exclude, str) else list(exclude)
    dns: set[str] = set()
    for name in given:
        dn = name.strip().strip("/")
        if not dn:
            raise ValueError("an excluded DN cannot be empty")
        if "**" in dn:
            raise ValueError(
                f'"{dn}" cannot be excluded: "**" is not supported, and "*" matches within '
                "one RN only -- name the depth, as in uni/tn-*/BD-*"
            )
        dns.add(dn)
    return Exclusions(dns)


def _prune(index: dict[str, Any], excluded: Exclusions) -> None:
    """Drop the excluded MOs from one side's index, in place.

    Both sides are pruned, so an excluded MO is neither missing nor extra nor
    modified, whichever side happens to carry it: excluding something is saying
    nothing about it at all, and reporting it missing because the fabric was not
    consulted would be the comparison talking about what it did not look at.

    Pruning the index rather than filtering the report is what keeps the counts
    honest -- ``child_count`` is read off these same dicts, so a subtree with an
    excluded MO in it is reported one MO shorter.
    """

    if not excluded:
        return
    for dn in [dn for dn in index if excluded.covers(dn)]:
        del index[dn]


# -- the comparison --------------------------------------------------------


def _missing_and_modified(intended: Intended, actual: Tree, *, expand: bool) -> list[Change]:
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


def _extra(intended: Intended, actual: Tree, *, expand: bool) -> list[Change]:
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


def _under_a_missing_parent(dn: str, intended: Intended, actual: Tree) -> bool:
    """True when this MO's parent is missing too, so it goes with the parent.

    Only the parent is looked at: a grandparent that is missing makes the parent
    roll up in turn, so the whole subtree collapses onto its top MO.
    """

    parent = parent_dn(dn)
    return parent is not None and parent in intended.index and parent not in actual.index


def _under_an_extra_parent(dn: str, intended: Intended, actual: Tree) -> bool:
    """True when this MO's parent is extra too, so it goes with the parent."""

    parent = parent_dn(dn)
    return parent is not None and parent in actual.index and parent not in intended.index


def _compare(
    intended: dict[str, str], actual: dict[str, Any]
) -> dict[str, tuple[str | None, str | None]]:
    """Diff one MO's attributes both ways.

    An attribute the fabric carries and the configuration does not is reported
    with nothing on the right, the same way an MO only the fabric has is
    reported. The APIC returns an unset attribute as an empty string, and that
    is reported too: it is a value the configuration does not account for.
    """

    changed: dict[str, tuple[str | None, str | None]] = {}
    for key, value in intended.items():
        if key in _INSTRUCTION:
            continue
        raw = actual.get(key)
        before = None if raw is None else text(raw)
        if before != value:
            changed[key] = (before, value)
    for key, value in actual.items():
        if key in META or key in intended:
            continue
        changed[key] = (text(value), None)
    # Sorted so that the report does not depend on the order the attributes were
    # written in, nor on the order the APIC returned them.
    return dict(sorted(changed.items()))


def _only_intended(attributes: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
    return {
        key: (None, value) for key, value in sorted(attributes.items()) if key not in _INSTRUCTION
    }


def _only_actual(attributes: dict[str, Any]) -> dict[str, tuple[str | None, str | None]]:
    return {
        key: (text(value), None) for key, value in sorted(attributes.items()) if key not in META
    }
