# An ACI body names its children by a naming property (name, ip, tDn, ...) rather than
# by DN, so a DN is built from the class's RN format, which a4i._metadata bundles --
# never from what the fabric happens to carry.

from __future__ import annotations

import re
from collections.abc import Callable, Container, Iterable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from a4i._metadata import rn_format

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
    ``warning`` -- for :func:`a4i.dry_run` and :meth:`a4i.Client.dry_run`, and
    how the fabric stands against the intended configuration -- ``missing``,
    ``modified``, ``extra`` -- for :func:`a4i.diff`.
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


def child_dn(parent: str, class_name: str, body: dict[str, Any]) -> str | None:
    attributes = body.get("attributes") or {}
    dn = attributes.get("dn")
    if isinstance(dn, str) and dn.strip("/"):
        return dn.strip("/")
    rn = attributes.get("rn")
    if isinstance(rn, str) and rn:
        return f"{parent}/{rn}"
    fmt = rn_format(class_name)
    built = None if fmt is None else fill_rn(fmt, attributes)
    return None if built is None else f"{parent}/{built}"


def fill_rn(fmt: str, attributes: dict[str, Any]) -> str | None:
    missing = False

    def slot(match: re.Match[str]) -> str:
        nonlocal missing
        value = attributes.get(match[1])
        if value is None or not str(value):
            missing = True
            return ""
        return str(value)

    filled = _SLOT.sub(slot, fmt)
    return None if missing else filled


# A slot stands for a naming value, so it matches a "/" among its characters:
# subnet-[{ip}] writes subnet-[10.0.0.1/24].
def matches_rn(fmt: str, rn: str) -> bool:
    return _rn_pattern(fmt).fullmatch(rn) is not None


# Cached because a4i._merge weighs one RN against every format the dictionary holds, and
# compiling all of them again per gap is the whole cost of filling one in.
@cache
def _rn_pattern(fmt: str) -> re.Pattern[str]:
    parts = _SLOT.split(fmt)
    # _SLOT holds one group, so the split alternates literal, attribute name,
    # literal -- and the odd ones are the slots.
    pattern = "".join(".+" if index % 2 else re.escape(part) for index, part in enumerate(parts))
    return re.compile(pattern, re.DOTALL)


# A response is not an input, so an MO it does not name is left out rather than
# refused.
def top_level_dns(imdata: Any) -> list[str]:
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
    if not isinstance(mo, dict) or len(mo) != 1:
        return None
    ((class_name, body),) = mo.items()
    if not isinstance(body, dict):
        return None
    return class_name, body


def tail_rn(dn: str) -> str:
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
    rn = tail_rn(dn)
    if rn == dn:
        return None
    return dn[: -(len(rn) + 1)]


def split_rns(dn: str) -> list[str]:
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


# Walked with parent_dn rather than matched against the text of the DN:
# uni/tn-a/BD-b/subnet-[10.0.0.1 is a prefix of a real DN and an ancestor of nothing.
def is_under(dn: str, dns: Container[str]) -> bool:
    current: str | None = dn
    while current is not None:
        if current in dns:
            return True
        current = parent_dn(current)
    return False


# A DN of its own ends with a "]" often enough -- subnet-[10.0.0.1/24] -- so a trailing
# bracket group is a condition only when it holds a "=", which no ACI naming value does.
def split_condition(name: str) -> tuple[str, tuple[str, str] | None]:
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
    rns: tuple[Callable[[str], Any], ...]
    key: str
    value: Callable[[str], Any]

    def matches(self, rns: list[str], attributes: dict[str, Any]) -> bool:
        if len(self.rns) != len(rns):
            return False
        value = attributes.get(self.key)
        # An MO without the attribute is not a match: the condition asks what
        # the value is, and an absent one has no value to be.
        if value is None or not self.value(str(value)):
            return False
        return all(matches(one) for matches, one in zip(self.rns, rns, strict=True))


class _Names:
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
        if ancestor in self.literal:
            return True
        # The zip stops at the pattern's own length, which is this depth: what
        # it walks is the ancestor, not the whole DN.
        return any(
            all(matches(one) for matches, one in zip(pattern, rns, strict=False))
            for pattern in self.patterns.get(depth, ())
        )


# A "*" stands for any part of a single RN and never runs across a "/", which is the
# point of not reaching for fnmatch or re: an exclusion that covers a subtree by
# accident is a comparison that reports no difference, and fnmatch would read
# [10.0.0.1/24] as a set of characters besides.
#
# A name may end with one attribute condition, which no DN can answer, so resolve
# settles those against each side's MOs before covers is asked anything. Both sides are
# resolved and the matches pooled: dropping an MO from one side alone would report it
# missing or extra -- an exclusion inventing the difference it was written to quiet.
class Exclusions:
    def __init__(self, dns: Iterable[str] = ()) -> None:
        self._out = _Names()
        self._kept = _Names()
        for dn in dns:
            (self._kept if dn.startswith("!") else self._out).add(dn.removeprefix("!"))

    def resolve(self, index: dict[str, Any]) -> None:
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

    # The walk runs to the bottom rather than stopping at the first exclusion it meets:
    # a deeper name overrules a shallower one both ways.
    def covers(self, dn: str) -> bool:
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
    if "*" not in rn:
        return rn.__eq__
    # Everything either side of a "*" is escaped, so "*" is the one character a
    # pattern spells with. DOTALL for the same reason: nothing about a naming
    # value is a line.
    return re.compile(".*".join(re.escape(part) for part in rn.split("*")), re.DOTALL).fullmatch
