"""Fold several configurations into the one body they describe together.

:func:`read` absorbs the inputs into an index keyed by DN and :func:`merge`
writes that index back out. :mod:`a4i._diff` and :mod:`a4i._dry_run` read both of
their sides through :func:`read`, so what one of the three refuses the other two
refuse as well. Nothing here performs I/O: a DN follows from the body and the
bundled RN formats alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from a4i._metadata import describe, load_rn_formats, rn_format
from a4i._mo import (
    ROOT,
    WRAPPER,
    Exclusions,
    child_dn,
    matches_rn,
    parent_dn,
    split_mo,
    split_rns,
    tail_rn,
    text,
)
from a4i._validate import problems, refuse

# What the key says about an MO is not written again among its attributes, and
# "childAction" is the APIC talking. "status" is deliberately not here, unlike in
# a4i._mo.META: dropping it would be a configuration whose deletions had silently
# stopped working.
_DROPPED = frozenset({"dn", "rn", "childAction"})

# How many unidentified MOs to name before summarising the rest.
_NAMED = 3


def merge(*configs: Any, loose: bool = False) -> dict[str, Any]:
    """Return the single body ``configs`` describe between them.

    Each argument is an ACI body -- one MO, or a list of them -- read as
    describing the whole of ``uni``, and they are merged in the order given with
    later values winning attribute by attribute. The result is a ``polUni``
    holding every merged MO, each nested under the MO it hangs off, named by its
    ``rn``, and siblings in RN order.

    ``loose`` fills in an ancestor a DN names and no input describes, where the
    bundled dictionary settles what class sits there. It is off by default -- a
    filled-in ancestor is an MO the POST may create that no input asked for.

    Raises :class:`ValueError` if an input is not written as ACI expects, if an
    MO does not carry the properties its RN is built from, if the inputs describe
    no MO at all, or if an MO cannot be placed under ``uni``, and
    :class:`UndescribedError` for the one refusal ``loose`` lifts.
    """

    intended = read(*configs, loose=loose)
    if not intended.index:
        raise ValueError(
            "the configuration is empty: nothing given describes an MO. Check the paths -- "
            "a directory is searched for *.json, and a file holding {} or [] describes nothing."
        )
    return _body(intended)


def read(
    *configs: Any,
    loose: bool = False,
    excluded: Exclusions | None = None,
) -> Intended:
    """Return the index ``configs`` describe between them, refused or not at all.

    Everything :func:`merge` refuses is refused here. An empty index is not:
    what that means differs between a merge, a comparison and a fetch, so each
    caller says its own. ``excluded`` only ever quiets the complaint about an MO
    that cannot be named under an excluded parent.
    """

    refuse([p for i, config in enumerate(configs) for p in problems(config, f"configs[{i}]")])
    intended = Intended(excluded)
    for config in configs:
        intended.absorb(config)
    if intended.unidentified:
        raise ValueError(unidentified_message(intended.unidentified))
    _refuse_the_unplaceable(intended.index, loose=loose)
    return intended


def empty() -> dict[str, Any]:
    """Return the body that describes no MO at all, which :func:`merge` refuses to."""

    return {WRAPPER: {"attributes": {"dn": ROOT}, "children": []}}


def _body(intended: Intended) -> dict[str, Any]:
    """Write the index back out as one body to post at ``uni``.

    A tree, because that is the only shape a POST takes. What cannot be placed
    has been refused by :func:`read` already, so every DN here has the MO above
    it to hang on.
    """

    bodies: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    # Sorted, so a parent is written before anything under it and is there to
    # hang it on: a DN is a prefix of the DNs below it, and a prefix sorts
    # first. Within one parent this is RN order.
    for dn, node in sorted(intended.index.items()):
        attributes: dict[str, str] = {}
        if node.real_rn:
            attributes["rn"] = tail_rn(dn)
        attributes.update(node.attributes)
        body: dict[str, Any] = {"attributes": attributes}
        bodies[dn] = body
        parent = parent_dn(dn)
        if parent is None or parent == ROOT:
            roots.append({node.class_name: body})
        else:
            bodies[parent].setdefault("children", []).append({node.class_name: body})
    # The wrapper's "dn" is not merged from anything: it is this module's own
    # statement that the body belongs at uni.
    return {WRAPPER: {"attributes": {"dn": ROOT}, "children": roots}}


def count(body: dict[str, Any]) -> int:
    """Return how many MOs a merged body carries, the wrapper aside."""

    def below(children: Any) -> int:
        total = 0
        for child in children if isinstance(children, list) else []:
            parsed = split_mo(child)
            if parsed is None:
                continue
            total += 1 + below(parsed[1].get("children"))
        return total

    return below((body.get(WRAPPER) or {}).get("children"))


# -- what cannot be placed -------------------------------------------------


class UndescribedError(ValueError):
    """Nothing describes a DN on the way down to an MO, and it was not filled in.

    Told apart from the other refusals so that a caller can name its own way out
    of this one -- ``--loose``, ``loose: true``. ``count`` is how many DNs it is,
    so that what they add says "it" or "them" as the message itself does.
    """

    def __init__(self, message: str, count: int) -> None:
        super().__init__(message)
        self.count = count


def _refuse_the_unplaceable(index: dict[str, Mo], *, loose: bool) -> None:
    """Refuse what no single body posted at ``uni`` could carry.

    ``loose`` lifts the refusal of an undescribed ancestor and no other: an MO
    outside ``uni`` has no ancestor that would bring it in, and one written where
    it cannot hang is written there whatever its ancestors are.

    Containment is weighed last, so that what ``loose`` filled in is weighed
    with everything else: there is nothing to weigh a child against until an MO
    stands above it.
    """

    outside, undescribed = _unplaceable(index)
    if outside:
        raise ValueError(_outside_message(outside))
    if undescribed:
        if not loose:
            raise UndescribedError(_undescribed_message(undescribed, index), len(undescribed))
        left = _fill_the_undescribed(index, undescribed)
        if left:
            raise ValueError(_unfillable_message(left, index))
    records: dict[str, dict[str, Any]] = {}
    misplaced = _misplaced(index, records)
    if misplaced:
        raise ValueError(_misplaced_message(misplaced, records))


def _unplaceable(index: dict[str, Mo]) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return the MOs that sit outside ``uni``, and the DNs nothing describes."""

    outside: list[tuple[str, str]] = []
    # Each DN nothing describes, against one DN under it: one MO under a gap is
    # enough both to report it and to work out what class stands there.
    undescribed: dict[str, str] = {}
    for dn in sorted(index):
        ancestors: list[str] = []
        current = parent_dn(dn)
        while current is not None and current != ROOT:
            ancestors.append(current)
            current = parent_dn(current)
        if current is None:
            outside.append((index[dn].class_name, dn))
            continue
        for ancestor in ancestors:
            if ancestor not in index:
                undescribed.setdefault(ancestor, dn)
    return outside, undescribed


def _misplaced(
    index: dict[str, Mo], records: dict[str, dict[str, Any]]
) -> list[tuple[str, str, str]]:
    """Return the class, DN and containing class of each MO its container cannot hold.

    Every DN has its whole line of ancestors in the index by the time this runs,
    which is what the two refusals before it settle, so ``index[parent]`` is
    there to read.
    """

    misplaced: list[tuple[str, str, str]] = []
    for dn in sorted(index):
        parent = parent_dn(dn)
        container = WRAPPER if parent is None or parent == ROOT else index[parent].class_name
        if _denies(container, index[dn].class_name, records):
            misplaced.append((index[dn].class_name, dn, container))
    return misplaced


def _denies(container: str, class_name: str, records: dict[str, dict[str, Any]]) -> bool:
    """True where the dictionary says an MO of ``container`` cannot hold ``class_name``.

    Only what it settles outright. A class the dictionary has never heard of, and
    one it marks unconfigurable, are passed over on either side: a record lists
    the configurable children alone, so reading an absence there as a refusal
    would refuse over what the dictionary leaves out.
    """

    if not _configurable(class_name, records) or not _configurable(container, records):
        return False
    return not _holds(container, class_name, records)


# -- filling in what nothing describes -------------------------------------


def _fill_the_undescribed(index: dict[str, Mo], undescribed: dict[str, str]) -> dict[str, str]:
    """Put an MO in the index at each DN nothing describes, and return what is left.

    The class is read off the RN of the gap and the MOs that hang under it, and
    a gap the dictionary does not settle outright is handed back rather than
    guessed at. Deepest first, so a gap two levels up is read off the MO that
    was just filled in below it.
    """

    below: dict[str, list[str]] = {}
    for dn, node in index.items():
        parent = parent_dn(dn)
        if parent is not None:
            below.setdefault(parent, []).append(node.class_name)
    records: dict[str, dict[str, Any]] = {}
    left: dict[str, str] = {}
    for dn in sorted(undescribed, key=lambda gap: (len(split_rns(gap)), gap), reverse=True):
        class_name = _class_at(tail_rn(dn), below.get(dn, []), records)
        if class_name is None:
            left[dn] = undescribed[dn]
            continue
        index[dn] = Mo(class_name, dn)
        parent = parent_dn(dn)
        if parent is not None:
            below.setdefault(parent, []).append(class_name)
    return left


def _class_at(rn: str, below: Iterable[str], records: dict[str, dict[str, Any]]) -> str | None:
    """Return the one class that can be named ``rn`` and hold all of ``below``.

    Narrowed by what a class may hold and never by what a record says it hangs
    under: a record's parents are truncated examples, so reading them here would
    leave a class like ``tagAnnotation``, with its 2,866 of them, unable to narrow
    anything. None where the dictionary leaves more than one candidate standing.
    """

    candidates = [name for name, fmt in load_rn_formats().items() if matches_rn(fmt, rn)]
    for class_name in below:
        # Narrows nothing, for the reason _denies passes the same two over.
        if not _configurable(class_name, records):
            continue
        candidates = [name for name in candidates if _holds(name, class_name, records)]
        if not candidates:
            return None
    return candidates[0] if len(candidates) == 1 else None


def _configurable(class_name: str, records: dict[str, dict[str, Any]]) -> bool:
    return bool(_record(class_name, records).get("configurable"))


def _holds(class_name: str, child: str, records: dict[str, dict[str, Any]]) -> bool:
    return child in (_record(class_name, records).get("children") or ())


def _record(class_name: str, records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return ``class_name``'s dictionary record, read once each.

    :func:`a4i._metadata.describe` is a seek and a parse, and one class is asked
    about once per gap it is weighed against.
    """

    known = records.get(class_name)
    if known is None:
        known = records[class_name] = describe(class_name) or {}
    return known


def _outside_message(outside: list[tuple[str, str]]) -> str:
    named = ", ".join(f'{class_name} at "{dn}"' for class_name, dn in outside[:_NAMED])
    if len(outside) > _NAMED:
        named += f", and {len(outside) - _NAMED} more"
    one = len(outside) == 1
    return (
        f"cannot fold {named} into one body: a merged body is posted at {ROOT}, and "
        f"{'this DN does' if one else 'these DNs do'} not sit under it. Post "
        f'{"it" if one else "each of them"} on its own with "a4i post mo".'
    )


def _undescribed_message(undescribed: dict[str, str], index: dict[str, Mo]) -> str:
    missing = sorted(undescribed)
    named = ", ".join(
        f'"{dn}" ({index[undescribed[dn]].class_name} at "{undescribed[dn]}" hangs under it)'
        for dn in missing[:_NAMED]
    )
    if len(missing) > _NAMED:
        named += f", and {len(missing) - _NAMED} more"
    one = len(missing) == 1
    return (
        f"nothing describes {named}: a merged body nests every MO under the MO it hangs "
        f"off, so every DN on the way down from {ROOT} has to be described. Add "
        f"{'it' if one else 'them'}, or drop what hangs under "
        f"{'it' if one else 'them'}."
    )


def _unfillable_message(left: dict[str, str], index: dict[str, Mo]) -> str:
    missing = sorted(left)
    named = ", ".join(
        f'"{dn}" ({index[left[dn]].class_name} at "{left[dn]}" hangs under it)'
        for dn in missing[:_NAMED]
    )
    if len(missing) > _NAMED:
        named += f", and {len(missing) - _NAMED} more"
    one = len(missing) == 1
    return (
        f"nothing describes {named}, and the bundled dictionary does not settle what class "
        f"sits there: no one configurable class is written that way and may hold what hangs "
        f"under {'it' if one else 'them'}. Write {'it' if one else 'them'} out, or drop what "
        f"hangs under {'it' if one else 'them'}."
    )


def _hangs_under(class_name: str, records: dict[str, dict[str, Any]]) -> str:
    """Say where the dictionary has ``class_name`` hanging, or "" where it does not.

    A count of the rest, not a promise that the list is all of them: a record's
    parents are truncated.
    """

    record = _record(class_name, records)
    parents = list(record.get("parents") or ())
    if not parents:
        return ""
    named = ", ".join(parents[:_NAMED])
    rest = len(parents) - _NAMED + (record.get("moreParents") or 0)
    return f"{named}, and {rest} more" if rest > 0 else named


def _misplaced_message(
    misplaced: list[tuple[str, str, str]], records: dict[str, dict[str, Any]]
) -> str:
    named = ", ".join(
        f'{class_name} at "{dn}" (it hangs under {where}, not {container})'
        if (where := _hangs_under(class_name, records))
        else f'{class_name} at "{dn}" ({container} does not hold it)'
        for class_name, dn, container in misplaced[:_NAMED]
    )
    if len(misplaced) > _NAMED:
        named += f", and {len(misplaced) - _NAMED} more"
    one = len(misplaced) == 1
    return (
        f"cannot fold {named} into one body: a merged body nests every MO under the MO it "
        f"hangs off, and the APIC reads a child MO against what its parent may contain. "
        f"Move {'it' if one else 'each of them'} under the MO it hangs off, or drop "
        f"{'it' if one else 'them'}."
    )


# -- the merged tree -------------------------------------------------------


@dataclass
class Mo:
    """An MO the configuration asks for, merged across every input naming it."""

    class_name: str
    dn: str
    attributes: dict[str, str] = field(default_factory=dict)
    # False where the last RN is the stand-in a4i._mo.pseudo_rn builds. It keys
    # the merge as well as a real RN does, but writing one back out would be
    # writing an RN no POST could carry.
    real_rn: bool = True


def _names_its_own_rn(class_name: str, body: dict[str, Any]) -> bool:
    attributes = body.get("attributes") or {}
    dn = attributes.get("dn")
    if isinstance(dn, str) and dn.strip("/"):
        return True
    rn = attributes.get("rn")
    if isinstance(rn, str) and rn:
        return True
    return rn_format(class_name) is not None


class Intended:
    """The configurations merged into one tree, keyed by DN as the fabric's is.

    ``excluded`` is for :mod:`a4i._diff` alone, and only ever quiets the
    complaint about an MO that cannot be identified. :func:`merge` excludes
    nothing: dropping an MO from a body would be dropping configuration.
    """

    def __init__(self, excluded: Exclusions | None = None) -> None:
        self._excluded = Exclusions() if excluded is None else excluded
        self.index: dict[str, Mo] = {}
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
                # The class settles it, so a wrapper written without a "dn" is
                # read through just the same.
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
            if key not in _DROPPED
        }
        node = self.index.get(dn)
        if node is None:
            self.index[dn] = Mo(class_name, dn, attributes, _names_its_own_rn(class_name, body))
        else:
            # Attribute by attribute, so an attribute a later input is silent
            # about keeps the earlier value -- "status" included.
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
        """

        if identified:
            return False
        if not self._excluded.covers(parent):
            self.unidentified.append((class_name, parent, rn_format(class_name) or ""))
        return True

    def descendant_count(self, dn: str) -> int:
        prefix = f"{dn}/"
        return sum(1 for key in self.index if key.startswith(prefix))


def unidentified_message(unidentified: Iterable[tuple[str, str, str]]) -> str:
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
