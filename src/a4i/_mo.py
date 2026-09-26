"""Turning an MO body into a DN, and the shape of a difference in one.

An ACI body names its children by a naming property (``name``, ``ip``, ``tDn``,
...) rather than by DN, so a DN is built from the class's RN format, which
:mod:`a4i._metadata` bundles -- never from what the fabric happens to carry.
Both comparisons (:mod:`a4i._diff`, :mod:`a4i._dry_run`) are built on that reading
and report what they found as a :class:`Change`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Container, Iterable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from a4i._metadata import rn_format

# Attributes that identify the MO or steer the request, never a configuration
# value worth diffing.
META = frozenset({"dn", "rn", "status", "childAction"})

# The policy universe every configurable MO hangs under, and the class of the MO
# itself. Facts about the tree rather than about one command, which is why they
# sit here rather than in a4i._merge.
ROOT = "uni"
WRAPPER = "polUni"

# What an RN format puts an attribute value in: "BD-{name}".
_SLOT = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class Change:
    """One MO-level difference, in either comparison.

    ``kind`` is what a POST would do -- ``created``, ``modified``, ``deleted``,
    ``warning`` -- for :func:`a4i._dry_run.compare`, and how the fabric stands
    against the intended configuration -- ``missing``, ``modified``, ``extra``
    -- for :func:`a4i._diff.compare`.
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


def child_dn(parent: str, class_name: str, body: dict[str, Any]) -> tuple[str, bool]:
    """Return the DN a body's child MO refers to under ``parent``, and whether it names one.

    False only where an RN format is known and the body does not give what it
    fills from: a class the dictionary has never heard of gets a stand-in RN and
    True with it, being the dictionary falling short rather than the input.
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

    An empty value counts as missing, not as a value.
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


def matches_rn(fmt: str, rn: str) -> bool:
    """True when ``rn`` is an RN ``fmt`` writes: the reading of :func:`fill_rn`.

    A slot stands for a naming value, so it matches one character at least and a
    "/" among them: ``subnet-[{ip}]`` writes ``subnet-[10.0.0.1/24]``. Every
    other character of a format is itself.
    """

    return _rn_pattern(fmt).fullmatch(rn) is not None


@cache
def _rn_pattern(fmt: str) -> re.Pattern[str]:
    """The compiled reading of one RN format.

    Cached because :mod:`a4i._merge` weighs one RN against every format the
    dictionary holds, and compiling all of them again per gap is the whole cost
    of filling one in.
    """

    parts = _SLOT.split(fmt)
    # _SLOT holds one group, so the split alternates literal, attribute name,
    # literal -- and the odd ones are the slots.
    pattern = "".join(".+" if index % 2 else re.escape(part) for index, part in enumerate(parts))
    return re.compile(pattern, re.DOTALL)


def pseudo_rn(class_name: str, attributes: dict[str, Any]) -> str:
    """Return a stand-in RN for an MO whose real one cannot be worked out.

    ``fvCtx[name=vrf1]``. ACI writes no RN as ``key=value``, so a stand-in cannot
    be mistaken for one the APIC would return. Falling back to every attribute
    rather than to fewer keeps two inputs from merging unless they say the very
    same thing: an MO reported twice is a nuisance, one silently merged away is a
    fabric that reads as matching.
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


def top_level_dns(imdata: Any) -> list[str]:
    """Return the DNs of the MOs at the top level of a GET response, sorted.

    Nothing is built here, unlike in :func:`child_dn`: a response is not an
    input, so an MO it does not name is left out rather than given a stand-in.
    """

    dns: set[str] = set()
    for child in imdata if isinstance(imdata, list) else []:
        parsed = split_mo(child)
        if parsed is None:
            continue
        dn = (parsed[1].get("attributes") or {}).get("dn")
        if isinstance(dn, str) and dn.strip("/"):
            dns.add(dn.strip("/"))
    return sorted(dns)


def split_mo(mo: Any) -> tuple[str, dict[str, Any]] | None:
    """Return (class name, body) for a well-formed ``{"class": {...}}`` MO."""

    if not isinstance(mo, dict) or len(mo) != 1:
        return None
    ((class_name, body),) = mo.items()
    if not isinstance(body, dict):
        return None
    return class_name, body


def tail_rn(dn: str) -> str:
    """Return the last RN of ``dn``, the "/" inside brackets being naming values."""

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


def split_rns(dn: str) -> list[str]:
    """Split ``dn`` into its RNs, as :func:`tail_rn` splits off the last one.

    ``uni/tn-a/BD-b/subnet-[10.0.0.1/24]`` is four RNs and not five.
    """

    rns: list[str] = []
    depth = 0
    start = 0
    for position, char in enumerate(dn):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "/" and depth == 0:
            rns.append(dn[start:position])
            start = position + 1
    rns.append(dn[start:])
    return rns


def is_under(dn: str, dns: Container[str]) -> bool:
    """True when ``dn`` is one of ``dns``, or hangs under one of them.

    Walked with :func:`parent_dn` rather than matched against the text of the DN:
    ``uni/tn-a/BD-b/subnet-[10.0.0.1`` is a prefix of a real DN and an ancestor
    of nothing.
    """

    current: str | None = dn
    while current is not None:
        if current in dns:
            return True
        current = parent_dn(current)
    return False


def split_condition(name: str) -> tuple[str, tuple[str, str] | None]:
    """Split ``dn[key=value]`` into the DN and its attribute condition, if any.

    A DN of its own ends with a "]" often enough -- ``subnet-[10.0.0.1/24]`` --
    so a trailing bracket group is a condition only when it holds a "=", which no
    ACI naming value does.
    """

    if not name.endswith("]"):
        return name, None
    start = name.rfind("[")
    if start < 0:
        return name, None
    key, sep, value = name[start + 1 : -1].partition("=")
    if not sep:
        return name, None
    return name[:start], (key, value)


@dataclass
class _Conditioned:
    """A name that also holds an attribute condition, settled by :meth:`Exclusions.resolve`."""

    rns: tuple[Callable[[str], Any], ...]
    key: str
    value: Callable[[str], Any]

    def matches(self, rns: list[str], attributes: dict[str, Any]) -> bool:
        if len(self.rns) != len(rns):
            return False
        value = attributes.get(self.key)
        # An MO without the attribute is not a match: the condition asks what
        # the value is, and an absent one has no value to be.
        if value is None or not self.value(text(value)):
            return False
        return all(matches(one) for matches, one in zip(self.rns, rns, strict=True))


class _Names:
    """One side of an exclusion list: the DNs named outright and the patterns."""

    def __init__(self) -> None:
        self.literal: set[str] = set()
        # Patterns by the number of RNs they hold. A pattern matches a DN of
        # that depth alone, so this is both how one is found and what keeps
        # every MO from being held against every pattern.
        self.patterns: dict[int, list[tuple[Callable[[str], Any], ...]]] = {}
        self.conditioned: list[_Conditioned] = []

    def __bool__(self) -> bool:
        return bool(self.literal or self.patterns or self.conditioned)

    def add(self, name: str) -> None:
        dn, condition = split_condition(name)
        if condition is not None:
            key, value = condition
            self.conditioned.append(
                _Conditioned(tuple(_rn_match(rn) for rn in split_rns(dn)), key, _rn_match(value))
            )
            return
        if "*" not in dn:
            self.literal.add(dn)
            return
        rns = split_rns(dn)
        self.patterns.setdefault(len(rns), []).append(tuple(_rn_match(rn) for rn in rns))

    def holds(self, ancestor: str, depth: int, rns: list[str]) -> bool:
        """True when this side names ``ancestor``, the DN of ``rns`` cut at ``depth``."""

        if ancestor in self.literal:
            return True
        # The zip stops at the pattern's own length, which is this depth: what
        # it walks is the ancestor, not the whole DN.
        return any(
            all(matches(one) for matches, one in zip(pattern, rns, strict=False))
            for pattern in self.patterns.get(depth, ())
        )


class Exclusions:
    """The MOs a comparison leaves out, each named by a DN, a pattern or a "!" exception.

    A "*" stands for any part of a single RN and never runs across a "/", which
    is the point of not reaching for :mod:`fnmatch` or :mod:`re`: an exclusion
    that covers a subtree by accident is a comparison that reports no difference,
    and fnmatch would read ``[10.0.0.1/24]`` as a set of characters besides.

    A name may end with one attribute condition, which no DN can answer, so
    :meth:`resolve` settles those against each side's MOs before :meth:`covers`
    is asked anything. Both sides are resolved and the matches pooled: dropping
    an MO from one side alone would report it missing or extra -- an exclusion
    inventing the difference it was written to quiet.
    """

    def __init__(self, dns: Iterable[str] = ()) -> None:
        self._out = _Names()
        self._kept = _Names()
        for dn in dns:
            (self._kept if dn.startswith("!") else self._out).add(dn.removeprefix("!"))

    def resolve(self, index: dict[str, Any]) -> None:
        """Settle every conditioned name against one side's MOs, in place.

        What one matched is added to the DNs named outright, so every later
        question is the DN question it always was -- the ancestor walk included.
        """

        if not (self._out.conditioned or self._kept.conditioned):
            return
        for dn, node in index.items():
            rns = split_rns(dn)
            for names in (self._out, self._kept):
                for one in names.conditioned:
                    if one.matches(rns, node.attributes):
                        names.literal.add(dn)

    def __bool__(self) -> bool:
        # Exceptions on their own exclude nothing, and leave a comparison with
        # no pruning to do.
        return bool(self._out)

    def covers(self, dn: str) -> bool:
        """True when ``dn`` is left out, whether named outright or by a pattern.

        With nothing but DNs to go on this is :func:`is_under` and no more.
        Otherwise the DN is split once and every name read off that one walk,
        which runs to the bottom rather than stopping at the first exclusion it
        meets: a deeper name overrules a shallower one both ways.
        """

        if not (self._out.patterns or self._kept):
            return is_under(dn, self._out.literal)
        rns = split_rns(dn)
        covered = False
        ancestor = ""
        for depth, rn in enumerate(rns, start=1):
            ancestor = rn if depth == 1 else f"{ancestor}/{rn}"
            if self._kept.holds(ancestor, depth, rns):
                covered = False
            elif self._out.holds(ancestor, depth, rns):
                covered = True
        return covered


def _rn_match(rn: str) -> Callable[[str], Any]:
    """Return what tells whether an MO's RN matches this RN of a pattern.

    An RN with no "*" in it is compared for equality rather than compiled: most
    of a pattern is literal, and the whole of a DN named outright is.
    """

    if "*" not in rn:
        return rn.__eq__
    # Everything either side of a "*" is escaped, so "*" is the one character a
    # pattern spells with. DOTALL for the same reason: nothing about a naming
    # value is a line.
    return re.compile(".*".join(re.escape(part) for part in rn.split("*")), re.DOTALL).fullmatch


def text(value: Any) -> str:
    """ACI attribute values are strings; anything else is compared as one."""

    return value if isinstance(value, str) else str(value)
