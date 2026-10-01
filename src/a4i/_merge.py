# a4i._diff and a4i._dry_run read both of their sides through read, so what one of the
# three refuses the other two refuse as well -- bar an undescribed ancestor, which
# a4i._dry_run and a4i._plan read with loose.

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
)
from a4i._validate import NAMED, listed, problems, read_body, refuse

# What the key says about an MO is not written again among its attributes, and
# "childAction" is the APIC talking. "status" is deliberately not here: dropping it
# would be a configuration whose deletions had silently stopped working.
_DROPPED = frozenset({"dn", "rn", "childAction"})
# What is left for a comparison to skip: "status" tells the APIC what to do with an
# MO, so the fabric never has a value to hold it against.
_INSTRUCTION = frozenset({"status"})

EMPTY = (
    "the configuration is empty: nothing given describes an MO. Check the paths -- "
    "a file holding {} or [] describes nothing."
)


def merge(*configs: str | Any, loose: bool = False) -> dict[str, Any]:
    """Return the single body ``configs`` describe between them.

    Each argument is an ACI body -- one MO, a list of them, or the same as JSON
    text -- read as describing the whole of ``uni``, and they are merged in the
    order given with later values winning attribute by attribute. The result is a ``polUni``
    holding every merged MO, each nested under the MO it hangs off, named by its
    ``rn``, and siblings in RN order.

    ``loose`` fills in an ancestor a DN names and no input describes, where the
    bundled dictionary settles what class sits there. It is off by default -- a
    filled-in ancestor is an MO the POST may create that no input asked for.

    Raises :class:`ValueError` if an input is not written as ACI expects, if an
    MO gives neither a ``rn``, a ``dn`` nor the properties the bundled dictionary
    builds its RN from, if the inputs describe
    no MO at all, or if an MO cannot be placed under ``uni``, and
    :class:`UndescribedError` for the one refusal ``loose`` lifts.
    """

    intended = read(*parse(configs), loose=loose)
    if not intended.index:
        raise ValueError(EMPTY)
    return write(intended)


def parse(configs: Iterable[str | Any]) -> list[Any]:
    parsed = []
    for i, config in enumerate(configs):
        try:
            parsed.append(read_body(config)[1] if isinstance(config, str) else config)
        except ValueError as exc:
            raise ValueError(f"configs[{i}]: {exc}") from None
    return parsed


# An empty index is not refused here: what that means differs between a merge, a
# comparison and a fetch, so each caller says its own.
def read(
    *configs: Any,
    loose: bool = False,
    excluded: Exclusions | None = None,
) -> Intended:
    refuse([p for i, config in enumerate(configs) for p in problems(config, f"configs[{i}]")])
    intended = Intended(excluded)
    for config in configs:
        intended.absorb(config)
    if intended.unidentified:
        raise ValueError(unidentified_message(intended.unidentified))
    _refuse_the_unplaceable(intended.index, loose=loose)
    return intended


def write(intended: Intended) -> dict[str, Any]:
    bodies: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    # Sorted, so a parent is written before anything under it and is there to
    # hang it on: a DN is a prefix of the DNs below it, and a prefix sorts
    # first. Within one parent this is RN order.
    for dn, node in sorted(intended.index.items()):
        body: dict[str, Any] = {"attributes": {"rn": tail_rn(dn), **node.attributes}}
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
    def below(children: Any) -> int:
        total = 0
        for child in children if isinstance(children, list) else []:
            parsed = split_mo(child)
            if parsed is None:
                continue
            total += 1 + below(parsed[1].get("children"))
        return total

    return below((body.get(WRAPPER) or {}).get("children"))


# Sorted so that a report does not depend on the order the attributes were written in,
# nor on the order the APIC returned them.
def changed(
    intended: dict[str, str], current: dict[str, str]
) -> dict[str, tuple[str | None, str | None]]:
    return {
        key: (current.get(key), value)
        for key, value in sorted(intended.items())
        if key not in _INSTRUCTION and current.get(key) != value
    }


def added(attributes: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
    return {
        key: (None, value) for key, value in sorted(attributes.items()) if key not in _INSTRUCTION
    }


def removed(attributes: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
    return {
        key: (value, None) for key, value in sorted(attributes.items()) if key not in _INSTRUCTION
    }


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


# loose lifts the refusal of an undescribed ancestor and no other: an MO outside uni has
# no ancestor that would bring it in, and one written where it cannot hang is written
# there whatever its ancestors are. Containment is weighed last, so that what loose
# filled in is weighed with everything else.
def _refuse_the_unplaceable(index: dict[str, Mo], *, loose: bool) -> None:
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
    misplaced: list[tuple[str, str, str]] = []
    for dn in sorted(index):
        parent = parent_dn(dn)
        container = WRAPPER if parent is None or parent == ROOT else index[parent].class_name
        if _denies(container, index[dn].class_name, records):
            misplaced.append((index[dn].class_name, dn, container))
    return misplaced


# Only what the dictionary settles outright. A class it has never heard of, and one it
# marks unconfigurable, are passed over on either side: a record lists the configurable
# children alone, so reading an absence there as a refusal would refuse over what the
# dictionary leaves out.
def _denies(container: str, class_name: str, records: dict[str, dict[str, Any]]) -> bool:
    if not _configurable(class_name, records) or not _configurable(container, records):
        return False
    return not _holds(container, class_name, records)


# -- filling in what nothing describes -------------------------------------


# A gap the dictionary does not settle outright is handed back rather than guessed at.
# Deepest first, so a gap two levels up is read off the MO that was just filled in below
# it.
def _fill_the_undescribed(index: dict[str, Mo], undescribed: dict[str, str]) -> dict[str, str]:
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
        index[dn] = Mo(class_name, dn, filled=True)
        parent = parent_dn(dn)
        if parent is not None:
            below.setdefault(parent, []).append(class_name)
    return left


# Narrowed by what a class may hold and never by what a record says it hangs under: a
# record's parents are truncated examples, so reading them here would leave a class like
# tagAnnotation, with its 2,866 of them, unable to narrow anything.
def _class_at(rn: str, below: Iterable[str], records: dict[str, dict[str, Any]]) -> str | None:
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


# a4i._metadata.describe is a seek and a parse, and one class is asked about once per
# gap it is weighed against.
def _record(class_name: str, records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    known = records.get(class_name)
    if known is None:
        known = records[class_name] = describe(class_name) or {}
    return known


def _outside_message(outside: list[tuple[str, str]]) -> str:
    named = listed(outside, lambda mo: f'{mo[0]} at "{mo[1]}"')
    one = len(outside) == 1
    return (
        f"cannot fold {named} into one body: a merged body is posted at {ROOT}, and "
        f"{'this DN does' if one else 'these DNs do'} not sit under it. Post "
        f'{"it" if one else "each of them"} on its own with "a4i post mo".'
    )


def _undescribed_message(undescribed: dict[str, str], index: dict[str, Mo]) -> str:
    missing = sorted(undescribed)
    named = listed(
        missing,
        lambda dn: (
            f'"{dn}" ({index[undescribed[dn]].class_name} at "{undescribed[dn]}" hangs under it)'
        ),
    )
    one = len(missing) == 1
    return (
        f"nothing describes {named}: a merged body nests every MO under the MO it hangs "
        f"off, so every DN on the way down from {ROOT} has to be described. Add "
        f"{'it' if one else 'them'}, or drop what hangs under "
        f"{'it' if one else 'them'}."
    )


def _unfillable_message(left: dict[str, str], index: dict[str, Mo]) -> str:
    missing = sorted(left)
    named = listed(
        missing, lambda dn: f'"{dn}" ({index[left[dn]].class_name} at "{left[dn]}" hangs under it)'
    )
    one = len(missing) == 1
    return (
        f"nothing describes {named}, and the bundled dictionary does not settle what class "
        f"sits there: no one configurable class is written that way and may hold what hangs "
        f"under {'it' if one else 'them'}. Write {'it' if one else 'them'} out, or drop what "
        f"hangs under {'it' if one else 'them'}."
    )


# A count of the rest, not a promise that the list is all of them: a record's parents
# are truncated.
def _hangs_under(class_name: str, records: dict[str, dict[str, Any]]) -> str:
    record = _record(class_name, records)
    parents = list(record.get("parents") or ())
    if not parents:
        return ""
    named = ", ".join(parents[:NAMED])
    rest = len(parents) - NAMED + (record.get("moreParents") or 0)
    return f"{named}, and {rest} more" if rest > 0 else named


def _misplaced_one(mo: tuple[str, str, str], records: dict[str, dict[str, Any]]) -> str:
    class_name, dn, container = mo
    where = _hangs_under(class_name, records)
    if where:
        return f'{class_name} at "{dn}" (it hangs under {where}, not {container})'
    return f'{class_name} at "{dn}" ({container} does not hold it)'


def _misplaced_message(
    misplaced: list[tuple[str, str, str]], records: dict[str, dict[str, Any]]
) -> str:
    named = listed(misplaced, lambda mo: _misplaced_one(mo, records))
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
    class_name: str
    dn: str
    attributes: dict[str, str] = field(default_factory=dict)
    # Put there by loose rather than written by anyone: it says the MO is on the fabric
    # already, never that it should be.
    filled: bool = False


# excluded is for a4i._diff alone. merge excludes nothing: dropping an MO from a body
# would be dropping configuration.
class Intended:
    def __init__(self, excluded: Exclusions | None = None) -> None:
        self._excluded = Exclusions() if excluded is None else excluded
        self.index: dict[str, Mo] = {}
        # (class name, parent DN, RN format) of every MO the input does not say
        # enough about to name. Collected rather than raised on the spot, so that
        # one run names everything that has to be fixed.
        self.unidentified: list[tuple[str, str, str | None]] = []

    def absorb(self, config: Any) -> None:
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
            dn = child_dn(ROOT, class_name, body)
            if dn == ROOT:
                self._absorb_children(body, ROOT)
                continue
            if dn is None:
                self._unidentified(class_name, ROOT)
                continue
            self._absorb(class_name, body, dn)

    def _absorb(self, class_name: str, body: dict[str, Any], dn: str) -> None:
        attributes = {
            key: str(value)
            for key, value in (body.get("attributes") or {}).items()
            if key not in _DROPPED
        }
        node = self.index.get(dn)
        if node is None:
            self.index[dn] = Mo(class_name, dn, attributes)
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
            dn_of_child = child_dn(dn, child_class, child_body)
            if dn_of_child is None:
                self._unidentified(child_class, dn)
                continue
            self._absorb(child_class, child_body, dn_of_child)

    # Its children are not walked either: their keys hang off this one, so naming them
    # would only repeat this.
    def _unidentified(self, class_name: str, parent: str) -> None:
        if not self._excluded.covers(parent):
            self.unidentified.append((class_name, parent, rn_format(class_name)))

    def descendant_count(self, dn: str) -> int:
        prefix = f"{dn}/"
        return sum(1 for key in self.index if key.startswith(prefix))


def unidentified_message(unidentified: Iterable[tuple[str, str, str | None]]) -> str:
    # The same class under the same parent twice is one thing to fix, not two.
    unique = list(dict.fromkeys(unidentified))
    named = listed(
        unique,
        lambda mo: (
            f"{mo[0]} under {mo[1]} "
            + (
                "(a class the bundled dictionary lacks)"
                if mo[2] is None
                else f'(its RN is "{mo[2]}")'
            )
        ),
    )
    give = "Give it" if len(unique) == 1 else "Give each"
    return (
        f"cannot tell which MO the input means by {named}. "
        f'{give} a "dn", an "rn" or the properties its RN is built from.'
    )
