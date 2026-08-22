"""Fold several configurations into the one configuration they describe together.

A configuration is often written in pieces -- a base and the overrides for one
fabric, a directory with a file per tenant -- and both ``a4i diff`` and ``a4i
post`` want a single body. :func:`merge` is what turns the pieces into that body.

Two pieces name the same MO insofar as they resolve to the same DN, which takes
reading each tree down from its root rather than matching JSON against JSON. So
the inputs are absorbed into an index keyed by DN (:class:`Intended`), later
values winning attribute by attribute, and the index is written back out as one
body -- as a tree again, because that is the only shape the APIC takes a POST
in. :mod:`a4i.diff` shares the index and skips the writing back out.

Nothing here performs I/O, and nothing here reaches the fabric: a DN follows
from the body and the bundled RN formats alone. The output is therefore a
function of the inputs, which is what makes it worth keeping in git next to
them. :mod:`a4i.config` is where the files those inputs came from are opened,
and where the merged body is written back.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from a4i.metadata import describe, rn_format
from a4i.mo import (
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
from a4i.validate import problems, refuse

# ROOT and WRAPPER are a4i.mo's. Every intended MO hangs under the policy
# universe, so a root MO with no "dn" of its own is resolved as a child of ROOT,
# and that is also where the merged body is meant to be posted -- which is why
# the body says so: see _body. A POST to /uni is written wrapped in a WRAPPER,
# so an input that serves both commands carries one, and it is read through to
# its children rather than kept: uni is not configuration.

# Attributes dropped on the way in. "dn" and "rn" are how an input says which MO
# it means, and the answer to that is the key -- carrying them further would
# leave the body with two ways to say where an MO sits, of which only one had
# been merged. What goes back out is written from the key alone: see _body.
# "childAction" is the APIC talking, and has no business in an intended
# configuration.
#
# "status" is deliberately not in here, unlike in a4i.mo.META: it is an
# instruction to the APIC rather than a property of the fabric, and a merged
# body that lost it would be a configuration whose deletions had silently
# stopped working. The comparison in a4i.diff leaves it out of its own reckoning
# instead.
_DROPPED = frozenset({"dn", "rn", "childAction"})

# How many unidentified MOs to name before summarising the rest.
_NAMED = 3


def merge(*configs: Any, loose: bool = False) -> dict[str, Any]:
    """Return the single body ``configs`` describe between them.

    Each argument is an ACI body -- one MO, or a list of them -- read as
    describing the whole of ``uni``, and they are merged in the order given with
    later values winning attribute by attribute. So a file can be split into a
    base and an override without repeating the whole MO, and ``status`` carries
    from wherever it was last written.

    The result is a ``polUni`` holding every merged MO, each nested under the MO
    it hangs off and named by its ``rn``. It is what :meth:`a4i.Client.diff`
    compares against, and what a POST to ``uni`` takes -- the wrapper says as
    much by carrying ``dn: uni``.

    Siblings come out in RN order rather than in the order the inputs were read,
    which makes the output a function of the input set alone: adding one file
    changes the lines that file contributed and nothing else.

    Raises :class:`ValueError` if an input is not written as ACI expects (see
    :mod:`a4i.validate`), if an MO does not carry the properties its RN is built
    from -- there is no telling which MO to merge it with -- if the inputs
    describe no MO at all, which is what an empty directory and a mistyped path
    both look like, or if an MO cannot be placed in the tree: see
    :func:`_refuse_the_unplaceable`.

    ``loose`` asks for the ancestor a DN names and no input describes to be
    filled in, where the bundled dictionary settles what class sits there: see
    :func:`_fill_the_undescribed`. It relaxes that one refusal and no other, and
    it is off by default -- a filled-in ancestor is an MO the POST may create
    that no input asked for.

    The shape is refused first and on its own. :mod:`a4i.config` has already
    checked whatever it read from a file, naming the file; this is what stands
    between the other callers -- a library, the MCP tool's inline bodies -- and
    an input nothing has looked at.
    """

    refuse([p for i, config in enumerate(configs) for p in problems(config, f"configs[{i}]")])
    intended = Intended()
    for config in configs:
        intended.absorb(config)
    if intended.unidentified:
        raise ValueError(unidentified_message(intended.unidentified))
    if not intended.index:
        raise ValueError(
            "the configuration is empty: nothing given describes an MO. Check the paths -- "
            "a directory is searched for *.json, and a file holding {} or [] describes nothing."
        )
    return _body(intended, loose=loose)


def _body(intended: Intended, *, loose: bool = False) -> dict[str, Any]:
    """Write the index back out as one body to post at ``uni``.

    The index is flat and the body is a tree, because a tree is the only shape a
    POST takes: the APIC reads a child MO against what its parent may contain,
    so an fvBD written beside its tenant rather than inside it is refused
    however right its DN is.

    What each MO carries is its ``rn`` and nothing else of the key. The nesting
    says where the MO sits, so an absolute ``dn`` would only be a second way to
    say the same thing -- and one that has to be rewritten in every descendant
    the day a tenant is renamed.
    """

    _refuse_the_unplaceable(intended.index, loose=loose)
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
    # statement that the body belongs at uni, so that a file found on its own
    # says where it goes.
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
    of this one: ``a4i merge`` says to pass ``--loose``, and the MCP tool says to
    pass ``loose: true``. What is refused, and why, is this module's; what the
    option is called is theirs. ``count`` is how many DNs it is, so that what
    they add says "it" or "them" as the message itself does.
    """

    def __init__(self, message: str, count: int) -> None:
        super().__init__(message)
        self.count = count


def _refuse_the_unplaceable(index: dict[str, Mo], *, loose: bool) -> None:
    """Refuse what no single body posted at ``uni`` could carry.

    An MO fails that in two ways. It sits outside ``uni`` altogether, and
    nesting it under the wrapper would be posting it somewhere it does not
    belong. Or something on the way down to it is a DN nothing describes, and
    there is no MO to nest it in.

    Neither is guessed at. An ancestor made up here would be an MO the POST
    created that no input ever asked for -- a tenant appearing on the fabric
    because a BD was written and its tenant was not. ``loose`` is asking for it
    anyway, and only for the second of the two: an MO outside ``uni`` has no
    ancestor that would bring it in, so it is refused whatever ``loose`` says.
    """

    outside, undescribed = _unplaceable(index)
    if outside:
        raise ValueError(_outside_message(outside))
    if not undescribed:
        return
    if not loose:
        raise UndescribedError(_undescribed_message(undescribed, index), len(undescribed))
    left = _fill_the_undescribed(index, undescribed)
    if left:
        raise ValueError(_unfillable_message(left, index))


def _unplaceable(index: dict[str, Mo]) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return the MOs that sit outside ``uni``, and the DNs nothing describes."""

    outside: list[tuple[str, str]] = []
    # Each DN nothing describes, against one DN under it: what is reported is
    # the line the configuration is missing, not each MO left hanging, because
    # writing that one line settles all of them. It is also where the filling
    # starts from, for the same reason -- one MO under the gap is enough to say
    # what the gap is.
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


# -- filling in what nothing describes -------------------------------------


def _fill_the_undescribed(index: dict[str, Mo], undescribed: dict[str, str]) -> dict[str, str]:
    """Put an MO in the index at each DN nothing describes, and return what is left.

    A DN says what its MO is called and not what class it is, and a body cannot
    be written without the class. It is read off the MOs that hang under the
    gap: the dictionary says what may sit above each of them, and the RN of the
    gap says which of those it is -- ``tn-t`` under an ``fvBD`` is an
    ``fvTenant`` and nothing else.

    Only a class the dictionary gives an RN format is weighed, which is to say
    only a configurable one, so what gets filled in is always an MO a POST could
    carry. Where more than one MO hangs under the gap they have to agree, and
    each narrows the answer: what is filled in is what every one of them allows.
    Anything the dictionary does not settle outright is left alone and handed
    back -- guessing here is guessing at what the POST creates.

    Deepest first, so a gap two levels up is read off the MO that was just
    filled in below it: an fvAEPg alone gives the fvAp, and the fvAp then gives
    the fvTenant.
    """

    below: dict[str, list[str]] = {}
    for dn, node in index.items():
        parent = parent_dn(dn)
        if parent is not None:
            below.setdefault(parent, []).append(node.class_name)
    parents: dict[str, list[str]] = {}
    left: dict[str, str] = {}
    for dn in sorted(undescribed, key=lambda gap: (len(split_rns(gap)), gap), reverse=True):
        class_name = _class_at(tail_rn(dn), below.get(dn, []), parents)
        if class_name is None:
            left[dn] = undescribed[dn]
            continue
        index[dn] = Mo(class_name, dn)
        parent = parent_dn(dn)
        if parent is not None:
            below.setdefault(parent, []).append(class_name)
    return left


def _class_at(rn: str, below: Iterable[str], parents: dict[str, list[str]]) -> str | None:
    """Return the one class that can be named ``rn`` and hold all of ``below``.

    None where the dictionary does not settle it: nothing hangs under the gap
    that the dictionary knows, no class it allows above them is written that
    way, or more than one is.
    """

    narrowed: set[str] | None = None
    for class_name in below:
        allowed = _parents_of(class_name, parents)
        # A class the dictionary has never heard of allows nothing and rules out
        # nothing: it is the dictionary falling short rather than a disagreement,
        # and a sibling it does know settles the gap just the same.
        if not allowed:
            continue
        written = {
            parent
            for parent in allowed
            if (fmt := rn_format(parent)) is not None and matches_rn(fmt, rn)
        }
        narrowed = written if narrowed is None else narrowed & written
        if not narrowed:
            return None
    if narrowed is None or len(narrowed) != 1:
        return None
    return next(iter(narrowed))


def _parents_of(class_name: str, parents: dict[str, list[str]]) -> list[str]:
    """Return the classes an MO of ``class_name`` may hang under, read once each.

    :func:`a4i.metadata.describe` is a seek and a parse of a record holding every
    property of the class, and one gap is asked about it once per MO under it.
    """

    known = parents.get(class_name)
    if known is None:
        record = describe(class_name) or {}
        known = [parent for parent in record.get("parents") or [] if isinstance(parent, str)]
        parents[class_name] = known
    return known


def _outside_message(outside: list[tuple[str, str]]) -> str:
    """Say which MOs do not sit under ``uni``, and what to do about them."""

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
    """Say which DNs the configuration has to describe before it can be folded."""

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
    """Say which DNs could not be filled in, and that they have to be written out."""

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


# -- the merged tree -------------------------------------------------------


@dataclass
class Mo:
    """An MO the configuration asks for, merged across every input naming it."""

    class_name: str
    dn: str
    attributes: dict[str, str] = field(default_factory=dict)
    # Whether the last RN of the DN is one the APIC would recognise, rather than
    # the stand-in :func:`a4i.mo.pseudo_rn` builds for a class the dictionary
    # has never heard of. A stand-in keys the merge as well as a real RN does,
    # but writing one back out would be writing an RN no POST could carry.
    real_rn: bool = True


def _names_its_own_rn(class_name: str, body: dict[str, Any]) -> bool:
    """True when the RN in this MO's key is one the APIC would recognise.

    A body giving a "dn" or an "rn" spells the RN itself, and a class the
    dictionary knows has a format to build one from. Failing both, the key came
    from :func:`a4i.mo.pseudo_rn`.
    """

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

    ``excluded`` is for :mod:`a4i.diff` alone, and only ever quiets the
    complaint about an MO that cannot be identified: which MO an input meant is
    a question about a subtree the comparison has been told to say nothing
    about, so there is nothing left for the input to settle. :func:`merge`
    excludes nothing -- it has no comparison to narrow, and dropping an MO from
    a body would be dropping configuration.
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
            if key not in _DROPPED
        }
        node = self.index.get(dn)
        if node is None:
            self.index[dn] = Mo(class_name, dn, attributes, _names_its_own_rn(class_name, body))
        else:
            # Later inputs win, attribute by attribute, so a file can be split
            # into a base and an override without repeating the whole MO. An
            # attribute the override is silent about keeps the base's value --
            # "status" included, so a base that deletes an MO goes on deleting
            # it unless an override says otherwise.
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

        One under an excluded parent is passed over rather than recorded, for
        the reason :class:`Intended` gives.
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
