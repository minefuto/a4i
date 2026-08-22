"""Refusing an input that is not a configuration, before anything reads it.

An MO the APIC returned and an MO someone wrote are the same JSON, and a
malformed one wants opposite treatment on the two sides. A response is a fact:
an element a4i cannot make sense of is a4i falling short, and reading past it
beats refusing to report the fabric at all. An input is an intention: an element
a4i cannot make sense of is a line someone wrote that will not take effect, and
reading past it is a configuration that quietly means something other than what
it says -- a BD that never reaches the fabric, or a fabric that reads as
matching because the MO it differs on was dropped on the way in.

So the leniency stays where the responses are read (:func:`a4i.mo.split_mo` goes
on returning None for what is not an MO), and every path that carries an input
-- ``merge``, ``diff``, ``post --dry-run`` -- runs it past :func:`problems`
first. Only a raw ``a4i post`` does not: it sends the body text untouched
without ever parsing it, and what the APIC makes of a malformed one is the
APIC's answer to give.

What is checked is the shape alone -- that each element is an MO, that a body
holds ``attributes`` and ``children``, that an attribute value is something ACI
could carry. Whether the MOs make sense together is :func:`a4i.merge.merge`'s
question and is asked afterwards, on an input already known to be well formed:
a diagnosis about a missing parent, drawn from a tree half of whose elements
were skipped, would be a diagnosis of the wrong thing.
"""

from __future__ import annotations

from typing import Any

from a4i.mo import ROOT, WRAPPER, child_dn

# What an MO body may hold, and nothing else.
_BODY_KEYS = frozenset({"attributes", "children"})

# How many problems to spell out before summarising the rest.
_NAMED = 3

_MO_SHAPE = 'an MO is one class name mapped to a body: {"fvBD": {"attributes": {"name": "bd1"}}}'
_BODY_SHAPE = 'an MO body is an object with "attributes" and an optional "children"'
_VALUE_SHAPE = "an ACI attribute value is a string"
_RESPONSE = 'this is a GET response, not a configuration. Pass what is inside "imdata".'


def problems(config: Any, source: str | None = None) -> list[str]:
    """Return what is wrong with ``config``, one line each, in reading order.

    ``source`` names where the input came from -- a file path, or ``configs[1]``
    for one body handed over in an argument -- and is written into every line,
    because a configuration is folded from several inputs and the one to fix is
    not otherwise apparent. Passing None leaves the lines naming a position
    alone, which is what one body handed to ``diff`` or ``post`` wants.

    An empty list means the input is well formed, and an input that describes
    nothing at all -- ``[]``, ``{}``, an empty ``polUni`` -- is well formed: a
    directory of files is merged, and a placeholder among them is not a mistake.
    What is refused is an element that was meant to be an MO and is not. Whether
    the inputs together describe any MO is :func:`a4i.merge.merge`'s to answer,
    and it does.
    """

    found: list[str] = []
    if isinstance(config, list):
        for index, element in enumerate(config):
            _element(element, source, f"[{index}]", ROOT, found)
    elif isinstance(config, dict):
        # The one empty that is not a mistake: an input describing nothing.
        if config:
            _element(config, source, "", ROOT, found)
    else:
        found.append(
            f"{_where(source, '', None)}: a configuration is an MO, an array of MOs, or a "
            f"polUni wrapping them, and this is {_kind(config)}."
        )
    return found


def refuse(found: list[str]) -> None:
    """Raise :class:`ValueError` reporting ``found``, or return if there is none.

    The first few are spelled out and the rest counted, as every other refusal
    in a4i does it. A count is worth more than the lines it stands for here: one
    mistyped element is one line to fix wherever it sits, so what the reader
    needs is a place to start and the knowledge that there is more of it.
    """

    if not found:
        return
    named = "\n".join(found[:_NAMED])
    if len(found) > _NAMED:
        named += f"\nand {len(found) - _NAMED} more"
    where = "" if len(found) == 1 else f", in {len(found)} places"
    raise ValueError(f"the configuration is not written as ACI expects{where}:\n{named}")


def check(config: Any, source: str | None = None) -> None:
    """Refuse ``config`` if anything is wrong with it. See :func:`problems`."""

    refuse(problems(config, source))


# -- walking the input -----------------------------------------------------


def _element(
    element: Any, source: str | None, path: str, parent: str | None, found: list[str]
) -> None:
    """Check one element that is meant to be an MO, and everything under it."""

    where = _where(source, path, parent)
    if not isinstance(element, dict):
        found.append(f"{where}: this element is {_kind(element)}, not an MO -- {_MO_SHAPE}.")
        return
    if not element:
        found.append(
            f"{where}: an empty object describes no MO. Drop it, or write the MO it stands for."
        )
        return
    if len(element) > 1:
        if "imdata" in element:
            found.append(f"{where}: {_RESPONSE}")
        else:
            found.append(
                f"{where}: {len(element)} keys ({_named(element)}), where {_MO_SHAPE}. "
                f"Write them as an array, one MO per element."
            )
        return
    ((class_name, body),) = element.items()
    if not isinstance(body, dict):
        if class_name == "imdata":
            found.append(f"{where}: {_RESPONSE}")
        else:
            found.append(
                f'{where}: "{class_name}" maps to {_kind(body)}, where {_BODY_SHAPE}. '
                f'Check the "attributes" level is there.'
            )
        return
    _body(class_name, body, source, path, parent, found)


def _body(
    class_name: str,
    body: dict[str, Any],
    source: str | None,
    path: str,
    parent: str | None,
    found: list[str],
) -> None:
    """Check an MO body, its attribute values, and its children."""

    where = _where(source, path, parent)
    before = len(found)
    unknown = [key for key in body if key not in _BODY_KEYS]
    if unknown:
        found.append(
            f'{where}: "{class_name}" carries {_quoted(unknown)}, which an MO body has no '
            f'place for -- it holds "attributes" and an optional "children" and nothing else.'
        )
    attributes = body.get("attributes")
    if attributes is not None and not isinstance(attributes, dict):
        found.append(
            f'{where}: "attributes" is {_kind(attributes)}, not an object of attribute '
            f"names and values."
        )
        attributes = None
    elif attributes:
        _attributes(attributes, where, found)
    children = body.get("children")
    if children is not None and not isinstance(children, list):
        found.append(f'{where}: "children" is {_kind(children)}, not an array of MOs.')
        children = None
    if not attributes and not children and class_name != WRAPPER and len(found) == before:
        # The wrapper is exempt: a polUni holding nothing is a file that
        # describes nothing, which is allowed. Every other class has to say
        # which MO it means, and an empty body says nothing at all.
        found.append(
            f'{where}: "{class_name}" gives neither attributes nor children, so there is '
            f"nothing to configure and no way to tell which MO it means."
        )
    # Worked out once, before the children add problems of their own: whether
    # this body is sound is a question about this body.
    dn = _dn_of(class_name, body, parent, sound=len(found) == before)
    for index, child in enumerate(children or []):
        _element(child, source, _under(path, index), dn, found)


def _dn_of(class_name: str, body: dict[str, Any], parent: str | None, *, sound: bool) -> str | None:
    """Return the DN to name this MO's children by, or None to name none of them.

    The DN is worked out the way :class:`a4i.merge.Intended` works it out, so
    that the position a problem is reported at is the position the merge would
    have put the MO at. None means it cannot be: something is already wrong with
    this body, or the input does not say which MO it means, and a DN built from
    that would be a place that does not exist.
    """

    if parent is None or not sound:
        return None
    if class_name == WRAPPER:
        return ROOT
    dn, identified = child_dn(parent, class_name, body)
    return dn if identified else None


def _attributes(attributes: dict[str, Any], where: str, found: list[str]) -> None:
    """Check that every attribute value is one ACI could carry.

    A string is what ACI carries and a number is written as one often enough to
    be worth accepting -- ``"mtu": 9000`` reaches the APIC as ``"9000"``.
    Everything else is refused rather than stringified: ``null`` would reach it
    as ``"None"`` and ``true`` as ``"True"``, neither of which is a value any
    property takes, and both of which a diff would go on reporting for ever.
    """

    for key, value in attributes.items():
        if isinstance(value, str) or (
            isinstance(value, int | float) and not isinstance(value, bool)
        ):
            continue
        fix = ""
        if isinstance(value, bool):
            fix = (
                ' Write it quoted -- "yes", "no", "true" or "false", whichever the property takes.'
            )
        elif value is None:
            fix = ' Write "" to clear the property, or leave the attribute out.'
        found.append(
            f'{where}: the "{key}" attribute is {_kind(value)}, where {_VALUE_SHAPE}.{fix}'
        )


# -- saying where ----------------------------------------------------------


def _where(source: str | None, path: str, parent: str | None) -> str:
    """Say where in the input something sits: the file, the position, the parent.

    The position is the one thing always there, and it is what an editor can be
    pointed at. The parent DN is added when there is one to add, because a file
    written as one tenant per element has a dozen positions that look alike and
    only the DN tells them apart. It is left off at the top level, where "child
    of uni" would be saying that every root MO hangs under uni.
    """

    text = f"{source}: {path}" if source and path else (source or path or "the body")
    if parent is not None and parent != ROOT:
        text += f" (child of {parent})"
    return text


def _under(path: str, index: int) -> str:
    return f"{path}.children[{index}]" if path else f"children[{index}]"


def _kind(value: Any) -> str:
    """Name the JSON type of ``value``, as the input spells it."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, list):
        return "an array"
    return "an object"


def _named(element: dict[str, Any]) -> str:
    return _quoted(list(element)[:_NAMED]) + (", ..." if len(element) > _NAMED else "")


def _quoted(keys: list[str]) -> str:
    return ", ".join(f'"{key}"' for key in keys)
