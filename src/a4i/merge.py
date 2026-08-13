"""Fold several configurations into the one configuration they describe together.

A configuration is often written in pieces -- a base and the overrides for one
fabric, a directory with a file per tenant -- and both ``a4i diff`` and ``a4i
post`` want a single body. :func:`merge` is what turns the pieces into that body.

Two pieces name the same MO insofar as they resolve to the same DN, which takes
reading each tree down from its root rather than matching JSON against JSON. So
the inputs are absorbed into an index keyed by DN (:class:`Intended`), later
values winning attribute by attribute, and the index is written back out as one
body. :mod:`a4i.diff` shares the index and skips the writing back out.

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

from a4i.metadata import rn_format
from a4i.mo import Exclusions, child_dn, split_mo, text

# Every intended MO hangs under the policy universe, so a root MO with no "dn"
# of its own is resolved as a child of this. It is also where the merged body is
# meant to be posted, which is why the body says so: see _body.
ROOT = "uni"

# The class of the policy universe itself. A POST to /uni is written wrapped in
# one, so an input that serves both commands carries it, and it is read through
# to its children rather than kept: uni is not configuration.
WRAPPER = "polUni"

# Attributes dropped on the way in. "dn" and "rn" are how an input says which MO
# it means, and the answer to that is the key -- carrying them further would
# leave the body with two ways to say where an MO sits, of which only one had
# been merged. "childAction" is the APIC talking, and has no business in an
# intended configuration.
#
# "status" is deliberately not in here, unlike in a4i.mo.META: it is an
# instruction to the APIC rather than a property of the fabric, and a merged
# body that lost it would be a configuration whose deletions had silently
# stopped working. The comparison in a4i.diff leaves it out of its own reckoning
# instead.
_DROPPED = frozenset({"dn", "rn", "childAction"})

# How many unidentified MOs to name before summarising the rest.
_NAMED = 3


def merge(*configs: Any) -> dict[str, Any]:
    """Return the single body ``configs`` describe between them.

    Each argument is an ACI body -- one MO, or a list of them -- read as
    describing the whole of ``uni``, and they are merged in the order given with
    later values winning attribute by attribute. So a file can be split into a
    base and an override without repeating the whole MO, and ``status`` carries
    from wherever it was last written.

    The result is a ``polUni`` wrapping every merged MO as a flat list of
    children, each carrying its own absolute ``dn``, sorted by that DN. It is
    what :meth:`a4i.Client.diff` compares against, and what a POST to ``uni``
    takes -- the wrapper says as much by carrying ``dn: uni``.

    Sorting by DN rather than keeping the order the inputs were read in makes
    the output a function of the input set alone, so adding one file changes the
    lines that file contributed and nothing else. It also puts a parent before
    its children, a DN being a prefix of the DNs under it.

    Raises :class:`ValueError` if an MO does not carry the properties its RN is
    built from -- there is no telling which MO to merge it with -- or if the
    inputs describe no MO at all, which is what an empty directory and a
    mistyped path both look like.
    """

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
    return _body(intended)


def _body(intended: Intended) -> dict[str, Any]:
    """Write the index back out as one body to post at ``uni``."""

    children = [
        {node.class_name: {"attributes": {"dn": dn, **node.attributes}}}
        for dn, node in sorted(intended.index.items())
    ]
    # The wrapper's "dn" is not merged from anything: it is this module's own
    # statement that the body belongs at uni, so that a file found on its own
    # says where it goes.
    return {WRAPPER: {"attributes": {"dn": ROOT}, "children": children}}


# -- the merged tree -------------------------------------------------------


@dataclass
class Mo:
    """An MO the configuration asks for, merged across every input naming it."""

    class_name: str
    dn: str
    attributes: dict[str, str] = field(default_factory=dict)


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
            self.index[dn] = Mo(class_name, dn, attributes)
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
