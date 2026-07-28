"""Compare a fabric's whole configuration against the one it is meant to have.

``a4i diff`` reads the intended configuration, fetches everything under ``uni``
from the APIC, and reports where the two disagree. Nothing in this module
performs I/O: it takes the parsed configurations and the ``imdata`` of those
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
there, and everything under it, is left out of the comparison altogether.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from a4i.metadata import rn_format
from a4i.mo import META, Change, Tree, child_dn, parent_dn, split_mo, text

# Every intended MO hangs under the policy universe, so a root MO with no "dn"
# of its own is resolved as a child of this.
ROOT = "uni"

# The class of the policy universe itself. A POST to /uni is written wrapped in
# one, so an input that serves both commands carries it, and it is read through
# to its children rather than compared: uni is not configuration.
WRAPPER = "polUni"

# How many unidentified MOs to name before summarising the rest.
_NAMED = 3


def compare(
    configs: Iterable[Any],
    imdata: Any,
    *,
    expand: bool = False,
    exclude: str | Sequence[str] | None = None,
) -> list[Change]:
    """Return how ``imdata`` differs from the intended configuration.

    ``configs`` are merged in order, later ones winning, into a single intended
    tree; ``imdata`` is everything under ``uni``. Without ``expand``, a subtree
    that is wholly missing or wholly extra is reported as its top MO alone, with
    the MOs below it counted rather than listed.

    ``exclude`` names MOs to leave out, each by its DN and each standing for
    everything under it as well. See :func:`_excluded_dns` for what a DN there
    means and :func:`_prune` for what leaving one out does.

    Raises :class:`ValueError` if an MO does not carry the properties its RN is
    built from, rather than comparing what is left: such a body names no one MO,
    and the one it meant may well be on the fabric. Reporting that one missing
    while reporting the real one extra is worse than saying what the input has to
    spell out.
    """

    excluded = _excluded_dns(exclude)
    actual = Tree(imdata)
    intended = _Intended(actual, excluded)
    for config in configs:
        intended.absorb(config)
    if intended.unidentified:
        raise ValueError(_unidentified_message(intended.unidentified))
    _prune(intended.index, excluded)
    _prune(actual.index, excluded)
    changes = _missing_and_modified(intended, actual, expand=expand)
    changes.extend(_extra(intended, actual, expand=expand))
    # A DN is reported at most once, so this orders the report without merging
    # anything: MOs read in tree order, and a tenant's changes stay together.
    changes.sort(key=lambda change: change.dn)
    return changes


# -- what is left out ------------------------------------------------------


def _excluded_dns(exclude: str | Sequence[str] | None) -> frozenset[str]:
    """Return the DNs to leave out, read the way any other DN is read.

    A single string is one DN rather than a list to split on something: an ACI
    naming value can hold a comma, so nothing here separates one DN from the
    next. A leading or trailing "/" is dropped, as :func:`a4i.query.build_path`
    drops it, and what is left of nothing but those is refused: an empty DN
    names no MO, and it is what an unset shell variable expands to.
    """

    if exclude is None:
        return frozenset()
    given = [exclude] if isinstance(exclude, str) else list(exclude)
    dns: set[str] = set()
    for name in given:
        dn = name.strip().strip("/")
        if not dn:
            raise ValueError("an excluded DN cannot be empty")
        dns.add(dn)
    return frozenset(dns)


def _is_excluded(dn: str, excluded: frozenset[str]) -> bool:
    """True when ``dn`` is excluded itself, or hangs under an MO that is.

    The ancestors are walked with :func:`a4i.mo.parent_dn` rather than matched
    against the text of the DN, because a naming value can hold a "/" of its
    own: ``uni/tn-a/BD-b/subnet-[10.0.0.1`` is a prefix of a real DN and an
    ancestor of nothing.
    """

    current: str | None = dn
    while current is not None:
        if current in excluded:
            return True
        current = parent_dn(current)
    return False


def _prune(index: dict[str, Any], excluded: frozenset[str]) -> None:
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
    for dn in [dn for dn in index if _is_excluded(dn, excluded)]:
        del index[dn]


# -- the intended tree -----------------------------------------------------


@dataclass
class _Mo:
    """An MO the configuration asks for, merged across every input naming it."""

    class_name: str
    dn: str
    attributes: dict[str, str] = field(default_factory=dict)


class _Intended:
    """The configurations merged into one tree, keyed by DN as the fabric's is.

    Merging happens here rather than on the raw JSON because two inputs name the
    same MO only insofar as they resolve to the same DN, which takes reading the
    tree down from its root.
    """

    def __init__(self, actual: Tree, excluded: frozenset[str] = frozenset()) -> None:
        self._actual = actual
        self._excluded = excluded
        self.index: dict[str, _Mo] = {}
        # (class name, parent DN, RN format) of every MO the input does not say
        # enough about to name. Collected rather than raised on the spot, so that
        # one run names everything that has to be fixed.
        self.unidentified: list[tuple[str, str, str]] = []

    def absorb(self, config: Any) -> None:
        """Merge one configuration in, its values winning over what is there."""

        for root in config if isinstance(config, list) else [config]:
            parsed = split_mo(root)
            if parsed is None:
                continue
            class_name, body = parsed
            if class_name == WRAPPER:
                # What hangs under uni are the roots. The class settles it, so a
                # wrapper written without a "dn" is read through just the same.
                self._absorb_children(body, ROOT)
                continue
            dn, identified = child_dn(ROOT, class_name, body)
            if dn == ROOT:
                self._absorb_children(body, ROOT)
                continue
            if self._unidentified(class_name, ROOT, identified):
                continue
            self._absorb(class_name, body, dn)

    def _absorb(self, class_name: str, body: dict[str, Any], dn: str) -> None:
        attributes = {
            key: text(value)
            for key, value in (body.get("attributes") or {}).items()
            if key not in META
        }
        node = self.index.get(dn)
        if node is None:
            self.index[dn] = _Mo(class_name, dn, attributes)
        else:
            # Later inputs win, attribute by attribute, so a file can be split
            # into a base and an override without repeating the whole MO.
            node.attributes.update(attributes)
        self._absorb_children(body, dn)

    def _absorb_children(self, body: dict[str, Any], dn: str) -> None:
        for child in body.get("children") or []:
            parsed = split_mo(child)
            if parsed is None:
                continue
            child_class, child_body = parsed
            dn_of_child, identified = child_dn(dn, child_class, child_body)
            if self._unidentified(child_class, dn, identified):
                continue
            self._absorb(child_class, child_body, dn_of_child)

    def _unidentified(self, class_name: str, parent: str, identified: bool) -> bool:
        """Record an MO the input does not name one of, and say so.

        Its children are not walked either: their keys hang off this one, so
        naming them would only repeat this.

        One under an excluded parent is passed over rather than recorded. Which
        MO it meant is a question about a subtree the comparison has been told
        to say nothing about, so there is nothing left for the input to settle.
        """

        if identified:
            return False
        if not _is_excluded(parent, self._excluded):
            self.unidentified.append((class_name, parent, rn_format(class_name) or ""))
        return True

    def descendant_count(self, dn: str) -> int:
        prefix = f"{dn}/"
        return sum(1 for key in self.index if key.startswith(prefix))


def _unidentified_message(unidentified: list[tuple[str, str, str]]) -> str:
    """Say which MOs the input does not name, and what to do about it."""

    # The same class under the same parent twice is one thing to fix, not two.
    unique = list(dict.fromkeys(unidentified))
    named = ", ".join(
        f'{class_name} under {parent} (its RN is "{fmt}")'
        for class_name, parent, fmt in unique[:_NAMED]
    )
    if len(unique) > _NAMED:
        named += f", and {len(unique) - _NAMED} more"
    give = "Give it" if len(unique) == 1 else "Give each"
    return (
        f"cannot tell which MO the input means by {named}: the properties an RN is "
        f'built from are missing. {give} those, a "dn" or an "rn".'
    )


# -- the comparison --------------------------------------------------------


def _missing_and_modified(intended: _Intended, actual: Tree, *, expand: bool) -> list[Change]:
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


def _extra(intended: _Intended, actual: Tree, *, expand: bool) -> list[Change]:
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


def _under_a_missing_parent(dn: str, intended: _Intended, actual: Tree) -> bool:
    """True when this MO's parent is missing too, so it goes with the parent.

    Only the parent is looked at: a grandparent that is missing makes the parent
    roll up in turn, so the whole subtree collapses onto its top MO.
    """

    parent = parent_dn(dn)
    return parent is not None and parent in intended.index and parent not in actual.index


def _under_an_extra_parent(dn: str, intended: _Intended, actual: Tree) -> bool:
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
    return {key: (None, value) for key, value in sorted(attributes.items())}


def _only_actual(attributes: dict[str, Any]) -> dict[str, tuple[str | None, str | None]]:
    return {
        key: (text(value), None) for key, value in sorted(attributes.items()) if key not in META
    }
