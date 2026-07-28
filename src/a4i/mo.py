"""Reading an MO tree the APIC returned, and the shape of a difference in one.

Both comparisons in this package start here: what a POST would change
(:mod:`a4i.dry_run`) and how a fabric differs from an intended configuration
(:mod:`a4i.diff`). Each flattens a GET response into a DN index, each has to
turn a body's child MO into a DN, and each reports what it found as a
:class:`Change`. That shared ground lives here; what the two make of it does not.

The hard part is that an ACI body names its children by a naming property
(``name``, ``ip``, ``tDn``, ...) rather than by DN. Turning that into a DN needs
the class's RN format, and those are bundled: :mod:`a4i.metadata` carries one per
configurable class, so a DN follows from the body alone and never from what the
fabric happens to be carrying.

A class the dictionary does not know is stood in for by :func:`pseudo_rn`, which
names the MO after the attributes the body does give. It keys as well as a real
RN would -- what it cannot do is be typed back into a query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from a4i.metadata import rn_format

# Attributes that identify the MO or steer the request, never a configuration
# value worth diffing.
META = frozenset({"dn", "rn", "status", "childAction"})

# What an RN format puts an attribute value in: "BD-{name}".
_SLOT = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class Change:
    """One MO-level difference, in either comparison.

    ``kind`` is what a POST would do -- ``created``, ``modified``, ``deleted``,
    ``warning`` -- for :func:`a4i.dry_run.compare`, and how the fabric stands
    against the intended configuration -- ``missing``, ``modified``, ``extra``
    -- for :func:`a4i.diff.compare`.
    """

    kind: str
    class_name: str
    dn: str
    # attribute -> (value now, value wanted). None on the left means the MO does
    # not carry the attribute today; None on the right means the intended
    # configuration does not mention it, which only a fabric-wide diff reports.
    attributes: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    # MOs under this one that go with it: deleted with it for a POST, and
    # missing or extra along with it for a diff.
    child_count: int = 0
    message: str = ""


@dataclass(frozen=True)
class Node:
    """An MO as the APIC returned it."""

    class_name: str
    dn: str
    attributes: dict[str, Any]


class Tree:
    """A GET response flattened into an index of MOs by DN."""

    def __init__(self, imdata: Any = None) -> None:
        self.index: dict[str, Node] = {}
        self.roots: list[Node] = []
        if imdata is not None:
            self.roots = self.add(imdata)

    def add(self, imdata: Any, parent: str | None = None) -> list[Node]:
        """Flatten a GET response into this tree, and return its top-level MOs.

        DNs are worked out with :func:`child_dn`, the same way an intended
        configuration's are, so that a response and a body naming the same MO
        key alike. It has to be the same rule on both sides: a comparison keys
        on the DN alone, so an MO the two read differently reads as one missing
        and another extra.
        """

        nodes: list[Node] = []
        for mo in imdata if isinstance(imdata, list) else []:
            parsed = split_mo(mo)
            if parsed is None:
                continue
            class_name, body = parsed
            dn = _returned_dn(parent, class_name, body)
            if dn is None:
                continue
            node = Node(class_name, dn, body.get("attributes") or {})
            self.index[dn] = node
            nodes.append(node)
            self.add(body.get("children") or [], dn)
        return nodes

    def descendant_count(self, dn: str) -> int:
        """Return how many MOs sit under ``dn``."""

        prefix = f"{dn}/"
        return sum(1 for key in self.index if key.startswith(prefix))


def _returned_dn(parent: str | None, class_name: str, body: dict[str, Any]) -> str | None:
    """Return the DN of an MO the APIC returned, or None when it cannot be placed.

    Only a top-level MO can fail: with no parent to hang an RN off, what the
    response calls it is all there is to go on.

    Below that :func:`child_dn` always yields a key, a stand-in one where the RN
    cannot be worked out. Standing one in is right for a response even though it
    is not for an input: the fabric returned this MO, so it is there, and
    dropping it would take everything under it along and read as a fabric
    missing the lot.
    """

    if parent is None:
        dn = (body.get("attributes") or {}).get("dn")
        if not isinstance(dn, str) or not dn.strip("/"):
            return None
        return dn.strip("/")
    return child_dn(parent, class_name, body)[0]


def child_dn(parent: str, class_name: str, body: dict[str, Any]) -> tuple[str, bool]:
    """Return the DN a body's child MO refers to under ``parent``, and whether it names one.

    An explicit ``dn`` or ``rn`` in the body wins. Failing that the class's RN
    format says how to build one: ``BD-{name}`` filled in from the attributes.
    Where the format calls for an attribute the body does not give, the RN is a
    :func:`pseudo_rn` and the second value is False -- the body names no one MO,
    and diff says so rather than comparing. A class the dictionary has never
    heard of gets a stand-in too, but True with it: an unknown class is the
    dictionary falling short, not the input.
    """

    attributes = body.get("attributes") or {}
    dn = attributes.get("dn")
    if isinstance(dn, str) and dn.strip("/"):
        return dn.strip("/"), True
    rn = attributes.get("rn")
    if isinstance(rn, str) and rn:
        return f"{parent}/{rn}", True
    fmt = rn_format(class_name)
    if fmt is None:
        return f"{parent}/{pseudo_rn(class_name, attributes)}", True
    built = fill_rn(fmt, attributes)
    if built is None:
        return f"{parent}/{pseudo_rn(class_name, attributes)}", False
    return f"{parent}/{built}", True


def fill_rn(fmt: str, attributes: dict[str, Any]) -> str | None:
    """Return ``fmt`` with its ``{attribute}`` slots filled in, or None if one is not.

    One expression covers every shape an RN takes: a fixed string (``rsctx``), a
    value (``BD-{name}``), a bracketed one (``subnet-[{ip}]``) and a compound
    (``from-[{from}]-to-[{to}]``).
    """

    missing = False

    def slot(match: re.Match[str]) -> str:
        nonlocal missing
        value = attributes.get(match[1])
        if value is None or not text(value):
            missing = True
            return ""
        return text(value)

    filled = _SLOT.sub(slot, fmt)
    return None if missing else filled


def pseudo_rn(class_name: str, attributes: dict[str, Any]) -> str:
    """Return a stand-in RN for an MO whose real one cannot be worked out.

    ``fvCtx[name=vrf1]``: the class, and what the input gave to tell this MO from
    its siblings. ACI builds an RN as a prefix and a value, never as ``key=value``,
    so a stand-in cannot be mistaken for one the APIC would return, nor collide
    with the real RN of a sibling.

    ``name`` names most classes, and using it alone is what lets one MO written
    across two inputs merge. Failing that every attribute goes in, so two inputs
    merge only when they say the very same thing: an MO reported twice is a
    nuisance, one silently merged away is a fabric that reads as matching.
    """

    name = attributes.get("name")
    if name is not None and text(name):
        return f"{class_name}[name={text(name)}]"
    values = ",".join(
        f"{key}={text(value)}"
        for key, value in sorted(attributes.items())
        if key not in META and value is not None and text(value)
    )
    return f"{class_name}[{values}]"


def split_mo(mo: Any) -> tuple[str, dict[str, Any]] | None:
    """Return (class name, body) for a well-formed ``{"class": {...}}`` MO."""

    if not isinstance(mo, dict) or len(mo) != 1:
        return None
    ((class_name, body),) = mo.items()
    if not isinstance(body, dict):
        return None
    return class_name, body


def tail_rn(dn: str) -> str:
    """Return the last RN of ``dn``.

    A naming value can hold a "/" of its own -- ``subnet-[10.0.0.1/24]`` -- so
    the separator is only a separator outside brackets.
    """

    depth = 0
    for position in range(len(dn) - 1, -1, -1):
        char = dn[position]
        if char == "]":
            depth += 1
        elif char == "[":
            depth -= 1
        elif char == "/" and depth == 0:
            return dn[position + 1 :]
    return dn


def parent_dn(dn: str) -> str | None:
    """Return the DN of ``dn``'s parent, or None when it has none."""

    rn = tail_rn(dn)
    if rn == dn:
        return None
    return dn[: -(len(rn) + 1)]


def text(value: Any) -> str:
    """ACI attribute values are strings; anything else is compared as one."""

    return value if isinstance(value, str) else str(value)
