# An MO the APIC returned and an MO someone wrote are the same JSON, and a malformed one
# wants opposite treatment: skipping an element of a response is a4i falling short of
# reporting the fabric, where skipping one of an input is a configuration that quietly
# means something other than what it says. So the leniency stays where responses are
# read (a4i._mo.split_mo goes on returning None) and every path carrying an input runs
# it past problems first -- bar a raw a4i post, which only checks that the body is JSON.
#
# The shape alone is checked. Whether the MOs make sense together is a4i._merge's
# question and is asked afterwards: a diagnosis drawn from a tree half of whose elements
# were skipped would be a diagnosis of the wrong thing.

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from a4i._mo import ROOT, WRAPPER, child_dn

# What an MO body may hold, and nothing else.
_BODY_KEYS = frozenset({"attributes", "children"})

# How many problems to spell out before summarising the rest.
NAMED = 3

_MO_SHAPE = 'an MO is one class name mapped to a body: {"fvBD": {"attributes": {"name": "bd1"}}}'
_BODY_SHAPE = 'an MO body is an object with "attributes" and an optional "children"'
_VALUE_SHAPE = "an ACI attribute value is a string"
_RESPONSE = 'this is a GET response, not a configuration. Pass what is inside "imdata".'


# Text is passed through untouched rather than reserialized, so the key order and the
# formatting reach the APIC exactly as the caller wrote them.
def read_body(body: str | Any) -> tuple[str, Any]:
    if not isinstance(body, str):
        return json.dumps(body), body
    if not body.strip():
        raise ValueError("empty body")
    try:
        return body, json.loads(body)
    except ValueError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from None


# An input that describes nothing at all -- [], {}, an empty polUni -- is well formed: a
# placeholder among a directory of files is not a mistake.
def problems(config: Any, source: str | None = None) -> list[str]:
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
    if not found:
        return
    named = listed(found, str, "\n")
    where = "" if len(found) == 1 else f", in {len(found)} places"
    raise ValueError(f"the configuration is not written as ACI expects{where}:\n{named}")


def listed(items: Sequence[Any], name: Callable[[Any], str], sep: str = ", ") -> str:
    text = sep.join(name(item) for item in items[:NAMED])
    if len(items) > NAMED:
        text += f"{sep}and {len(items) - NAMED} more"
    return text


# -- walking the input -----------------------------------------------------


def _element(
    element: Any, source: str | None, path: str, parent: str | None, found: list[str]
) -> None:
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


# Worked out the way a4i._merge.Intended works it out, so that a problem is reported at
# the position the merge would have put the MO at.
def _dn_of(class_name: str, body: dict[str, Any], parent: str | None, *, sound: bool) -> str | None:
    if parent is None or not sound:
        return None
    if class_name == WRAPPER:
        return ROOT
    return child_dn(parent, class_name, body)


# A number is accepted and reaches the APIC as a string. Everything else is refused
# rather than stringified: null would reach it as "None" and true as "True", which no
# property takes and a diff would go on reporting for ever.
def _attributes(attributes: dict[str, Any], where: str, found: list[str]) -> None:
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


# The parent DN is added because a file written as one tenant per element has a dozen
# positions that look alike.
def _where(source: str | None, path: str, parent: str | None) -> str:
    text = f"{source}: {path}" if source and path else (source or path or "the body")
    if parent is not None and parent != ROOT:
        text += f" (child of {parent})"
    return text


def _under(path: str, index: int) -> str:
    return f"{path}.children[{index}]" if path else f"children[{index}]"


def _kind(value: Any) -> str:
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
    return _quoted(list(element)[:NAMED]) + (", ..." if len(element) > NAMED else "")


def _quoted(keys: list[str]) -> str:
    return ", ".join(f'"{key}"' for key in keys)
