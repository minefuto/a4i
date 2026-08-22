"""Render APIC responses, the bundled model, and errors for the terminal.

This is the one module that touches ``rich``, which is what keeps the colour, the
terminal width and the ``--raw`` handling in a single place rather than in each
command that prints something.

What the terminal is arrives as a ``console`` rather than being looked up: the
functions that lay something out are told where it is going, because where it is
going decides what they produce. A caller that names none gets stdout, which is
what every command wants; a test names one over a string and reads back exactly
what a terminal of that width would have shown.

Two questions are asked of it, and they are not the same question.
:func:`_cut_width` asks how wide the thing being read is, which only a terminal
answers. :func:`_plain` asks whether to style, which ``--raw`` also settles. So
``--raw`` on a terminal drops the colour and keeps the fitting.

``rich`` is imported lazily: shell completion goes through ``cli`` but never
renders anything, and importing ``rich.console`` there costs more than the
completion lookup itself.
"""

from __future__ import annotations

import json
from collections import Counter
from functools import cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.console import Console

    from a4i.mo import Change

# The mark that opens a line, per kind of change, and the colour each mark is
# printed in. Attribute lines carry a mark of their own, so a line's colour
# always follows from the mark it starts with. A dry run and a diff report
# share the marks: what is on the fabric and not in the configuration reads the
# same as what a POST would delete.
_MARKS = {
    "created": "+",
    "missing": "+",
    "modified": "~",
    "deleted": "-",
    "extra": "-",
    "warning": "!",
}
_STYLES = {"+": "green", "~": "yellow", "-": "red", "!": "magenta"}

# The kinds each report counts, in the order the summary names them.
_DRY_RUN_KINDS = ("created", "modified", "deleted")
_DIFF_KINDS = ("missing", "modified", "extra")

# What the MOs under a reported one mean, for the kinds that carry a count.
_CHILD_NOTE = {"deleted": "deletes {}", "missing": "missing: {}", "extra": "extra: {}"}


@cache
def _console(*, stderr: bool = False) -> Console:
    """The console a caller gets when it names none.

    Cached because one process prints to one place. The functions below take a
    console of their own all the same: what the terminal is decides what they
    produce, so it is theirs to be told rather than theirs to go and look up.
    """

    from rich.console import Console

    return Console(stderr=stderr)


def _plain(console: Console, raw: bool) -> bool:
    """True when the output carries no styling: piped, redirected, or ``--raw``."""

    return raw or not console.is_terminal


def _write(console: Console, lines: list[str]) -> None:
    """Write plain lines out, past rich rather than through it.

    rich wraps what it prints to the console's width, which for anything but a
    terminal is a default 80 -- and a wrapped line is a JSON document broken in
    half and a DN split down the middle. Plain output is exactly what was built,
    so it goes to the file the console was opened on.
    """

    for line in lines:
        console.file.write(line + "\n")


def render(data: Any, *, raw: bool = False, console: Console | None = None) -> None:
    """Print ``data`` as JSON: colorized on a TTY, plain otherwise or with raw."""

    console = console or _console()
    if _plain(console, raw):
        _write(console, [json.dumps(data, indent=2, ensure_ascii=False)])
    else:
        console.print_json(data=data)


def render_dry_run(
    changes: list[Change],
    *,
    raw: bool = False,
    console: Console | None = None,
    stderr: bool = False,
) -> None:
    """Print the changes a POST would cause: colorized on a TTY, plain otherwise.

    ``stderr`` puts the report there instead, for a command whose standard
    output is the body the report is about: a report printed into a body is a
    body nothing can post.
    """

    _print(
        _report(changes, _DRY_RUN_KINDS, "no changes"),
        raw=raw,
        console=console or _console(stderr=stderr),
    )


def render_diff(
    changes: list[Change], *, raw: bool = False, console: Console | None = None
) -> None:
    """Print how the fabric differs from its intended configuration."""

    _print(_report(changes, _DIFF_KINDS, "no differences"), raw=raw, console=console or _console())


def dry_run_report(changes: list[Change]) -> str:
    """Return what :func:`render_dry_run` prints, as one string.

    The MCP server hands this to a model rather than to a terminal, and it is
    the same text either way: a report read in a chat and one read in a shell
    should not differ in what they say a POST would do.
    """

    return "\n".join(_report(changes, _DRY_RUN_KINDS, "no changes"))


def diff_report(changes: list[Change]) -> str:
    """Return what :func:`render_diff` prints, as one string."""

    return "\n".join(_report(changes, _DIFF_KINDS, "no differences"))


def _print(lines: list[str], *, raw: bool, console: Console) -> None:
    if _plain(console, raw):
        _write(console, lines)
        return
    for line in lines:
        # markup=False: a DN such as subnet-[10.0.0.1/24] is not rich markup.
        console.print(line, style=_line_style(line), markup=False, highlight=False)


def _line_style(line: str) -> str | None:
    return _STYLES.get(line.lstrip()[:1])


def _report(changes: list[Change], kinds: tuple[str, ...], empty: str) -> list[str]:
    if not changes:
        return [empty]
    lines: list[str] = []
    for change in changes:
        if lines:
            lines.append("")
        lines.extend(_change_lines(change))
    lines.append("")
    lines.append(_summary(changes, kinds))
    return lines


def _change_lines(change: Change) -> list[str]:
    header = f"{_MARKS.get(change.kind, ' ')} {change.class_name} {change.dn}"
    note = _CHILD_NOTE.get(change.kind)
    if note and change.child_count:
        header += f"  ({note.format(plural(change.child_count, 'child MO'))})"
    lines = [header]
    if change.message:
        lines.append(f"  {change.message}")
    for key, (before, after) in change.attributes.items():
        if before is None:
            lines.append(f"  + {key}: {_quote(after)}")
        elif after is None:
            lines.append(f"  - {key}: {_quote(before)}")
        else:
            lines.append(f"  ~ {key}: {_quote(before)} -> {_quote(after)}")
    return lines


def _summary(changes: list[Change], kinds: tuple[str, ...]) -> str:
    counts = Counter(change.kind for change in changes)
    parts = [f"{counts[kind]} {kind}" for kind in kinds]
    if counts["warning"]:
        parts.append(plural(counts["warning"], "warning"))
    return ", ".join(parts)


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _quote(value: str | None) -> str:
    return json.dumps(value, ensure_ascii=False)


# -- the bundled model ----------------------------------------------------
#
# What 'a4i search' and 'a4i describe' print. Neither reaches the fabric: both
# read the dictionary that ships with a4i. The MCP server serves the same records
# to a model as JSON and this lays them out for a person, which is the one place
# the two readers are deliberately given different things -- a model parses 8 KB
# without complaint, and a person wants the thirty properties they may set.

_ELLIPSIS = "…"

# Prose is wrapped at this, whatever the terminal is: a description read across
# 200 columns is read by moving your head.
_PROSE_WIDTH = 78

# How wide the middle column of each table may grow. In both tables the last
# column is prose, and a middle column that grows without bound leaves the prose
# nothing. The numbers come from the dictionary: half of all labels are under 30
# characters and the longest is 192, while a property's permitted values are
# already capped at 24 by the generator.
_LABEL_WIDTH = 30
_TYPE_WIDTH = 34

# The narrowest the last column may be before it is dropped instead of cut. Both
# tables put prose last because prose is what a narrow terminal can afford to
# lose -- a property's permitted values are what a body gets wrong, its
# description only helps you guess which property you wanted. Below this, what is
# left of a description is not a shorter description but a dangling fragment, and
# a column of those is worse than a table without one.
_MIN_PROSE = 20


def render_search(
    results: list[tuple[str, str, str]], *, raw: bool = False, console: Console | None = None
) -> None:
    """Print one matching class per line: its name, its label, its summary."""

    console = console or _console()
    _print_rich(_search_lines(results, _cut_width(console)), raw=raw, console=console)


def render_describe(
    record: dict[str, Any],
    *,
    all_props: bool = False,
    children: bool = False,
    raw: bool = False,
    console: Console | None = None,
) -> None:
    """Print what one class is for and what a body may set on it."""

    console = console or _console()
    _print_rich(
        _describe_lines(record, all_props=all_props, children=children, width=_cut_width(console)),
        raw=raw,
        console=console,
    )


def _cut_width(console: Console) -> int | None:
    """The column to cut a line at, or None when nothing is watching.

    Only a terminal has a width to overflow. Piped output is cut at nothing, so
    that a summary a person would have seen truncated still reaches grep whole.

    Deliberately not the same question as :func:`_plain`: ``--raw`` asks for the
    styling to go, not for the lines to stop fitting the terminal they are still
    being read in.
    """

    return console.width if console.is_terminal else None


def _print_rich(lines: list[Any], *, raw: bool, console: Console) -> None:
    """Print styled lines, dropping the styling when nobody can see it."""

    if _plain(console, raw):
        _write(console, [line.plain for line in lines])
        return
    for line in lines:
        # no_wrap: every line was already cut to the console's width, and a
        # second opinion from rich would fold the tables it just fitted.
        console.print(line, no_wrap=True)


def _cut(text: str, width: int | None) -> str:
    """Return ``text`` cut to ``width``, marking that something was dropped."""

    if width is None or len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + _ELLIPSIS


def _fits_prose(columns_before: int, width: int | None) -> bool:
    """True if a prose column starting here has room to be worth printing."""

    return width is None or width - columns_before >= _MIN_PROSE


def _wrapped(text: str, width: int | None) -> list[str]:
    import textwrap

    if not text:
        return []
    return textwrap.wrap(text, min(width or _PROSE_WIDTH, _PROSE_WIDTH))


def _search_lines(results: list[tuple[str, str, str]], width: int | None) -> list[Any]:
    from rich.text import Text

    name_width = max(len(name) for name, _, _ in results)
    label_width = min(max(len(label) for _, label, _ in results), _LABEL_WIDTH)
    summaries = _fits_prose(name_width + 2 + label_width + 2, width)
    lines = []
    for name, label, summary in results:
        row = f"{name:<{name_width}}  {_cut(label, label_width):<{label_width}}  "
        line = Text(_cut((row + summary if summaries else row).rstrip(), width))
        # The class name is the only part of this line that gets typed again.
        line.stylize("bold cyan", 0, len(name))
        lines.append(line)
    return lines


def _describe_lines(
    record: dict[str, Any], *, all_props: bool, children: bool, width: int | None
) -> list[Any]:
    from rich.text import Text

    name = record["class"]
    head = Text(name, style="bold cyan")
    if record.get("label"):
        head.append(f"  {record['label']}")
    if not record.get("configurable"):
        # Most of the dictionary is classes like this, so the missing rn and dn
        # lines below have to read as "this cannot be created" rather than as
        # something the record left out.
        head.append("  (read-only class)", style="dim")
    lines = [head]
    lines.extend(Text(line) for line in _wrapped(record.get("desc") or "", width))

    fields = _describe_fields(record)
    if fields:
        lines.append(Text(""))
        lines.extend(Text(_cut(field, width)) for field in fields)

    lines.append(Text(""))
    lines.extend(_property_lines(record, all_props=all_props, width=width))

    child_names = record.get("children")
    if child_names is not None:
        lines.append(_children_heading(child_names, shown=children))
        if children:
            lines.extend(Text(f"  {child}") for child in child_names)
    return lines


def _describe_fields(record: dict[str, Any]) -> list[str]:
    """Return the rn, dn and containment lines, for a class that has them."""

    fields = []
    if record.get("rn"):
        fields.append(f"rn  {record['rn']}")
    # A class reachable by more than three paths carries no dn at all: the
    # generator drops the list rather than print a dozen of them.
    for dn in record.get("dn") or []:
        fields.append(f"dn  {dn}")
    parents = record.get("parents") or []
    if parents:
        # The generator already stops at eight parents. Saying how many were
        # dropped keeps a class contained by forty from reading as one contained
        # by eight.
        more = record.get("moreParents")
        tail = f" and {more} more" if more else ""
        fields.append(f"in  {', '.join(parents)}{tail}")
    return fields


def _property_lines(record: dict[str, Any], *, all_props: bool, width: int | None) -> list[Any]:
    from rich.text import Text

    props = record.get("props") or {}
    if not props:
        return [Text("properties (none)")]
    if not record.get("configurable"):
        # A class nobody can configure carries each property as a bare type
        # string, every one of them read-only. Hiding those by default would
        # leave a heading with nothing under it, so they are always shown.
        typed = {name: {"type": type_name} for name, type_name in props.items()}
        lines = [Text(f"properties ({len(props)}, all read-only)")]
        lines.extend(_property_rows(typed, width=width))
        return lines

    settable = {name: prop for name, prop in props.items() if not prop.get("readOnly")}
    read_only = {name: prop for name, prop in props.items() if prop.get("readOnly")}
    heading = f"properties ({len(settable)} settable"
    if read_only:
        heading += f", {len(read_only)} read-only" + ("" if all_props else " hidden")
    lines = [Text(heading + ")")]
    lines.extend(_property_rows(settable, width=width))
    if read_only and all_props:
        lines.append(Text(""))
        lines.append(Text(f"read-only properties ({len(read_only)})"))
        lines.extend(_property_rows(read_only, width=width))
    return lines


def _property_rows(props: dict[str, dict[str, Any]], *, width: int | None) -> list[Any]:
    from rich.text import Text

    marked = {name: name + ("*" if prop.get("naming") else "") for name, prop in props.items()}
    types = {name: _cut(_property_type(prop), _TYPE_WIDTH) for name, prop in props.items()}
    name_width = max(len(value) for value in marked.values())
    type_width = max(len(value) for value in types.values())
    descriptions = _fits_prose(2 + name_width + 2 + type_width + 2, width)
    rows = []
    for name, prop in props.items():
        row = f"  {marked[name]:<{name_width}}  {types[name]:<{type_width}}  "
        desc = (prop.get("desc") or "") if descriptions else ""
        line = Text(_cut((row + desc).rstrip(), width))
        if prop.get("naming"):
            line.stylize("bold", 2 + len(name), 3 + len(name))
        # The description is the column the width eats first, so it is marked as
        # the lesser one rather than left to read like the rest of the row.
        if len(row) < len(line.plain):
            line.stylize("dim", len(row), len(line.plain))
        rows.append(line)
    return rows


def _property_type(prop: dict[str, Any]) -> str:
    """Return what a property accepts: its values, or its type and its bounds.

    The permitted values say more than the type does -- 'no|yes' over
    'scalar:Bool' -- so they win the column when there are any. Where there are
    none, the length a string is allowed to be is what a body gets wrong.
    """

    values = prop.get("values") or []
    if values:
        text = "|".join(values)
        if prop.get("moreValues"):
            text += "|" + _ELLIPSIS
    else:
        text = prop.get("type") or ""
        limits = _length_limits(prop)
        if limits:
            text += f" ({limits})"
    default = prop.get("default")
    return f"{text} = {default}" if default is not None else text


def _length_limits(prop: dict[str, Any]) -> str:
    """Return the bounds a value must fall between, when the model gives any.

    A handful of properties in the MIM carry a minimum above their maximum --
    fvCtx.vrfIndex is 1 to 0 -- which is a bound nothing can satisfy and so
    describes nothing. Printing it would send someone looking for the value that
    fits; the JSON still carries it for anyone who wants to see for themselves.
    """

    for validator in prop.get("validators") or []:
        low, high = validator.get("min"), validator.get("max")
        if low is not None and high is not None and low <= high:
            return f"{low}-{high}"
    return ""


def _children_heading(children: list[str], *, shown: bool) -> Any:
    from rich.text import Text

    line = Text(f"children ({len(children)})")
    if children and not shown:
        line.append("  --children to list them", style="dim")
    return line


def print_error(message: str) -> None:
    """Print an error message to stderr."""

    _console(stderr=True).print(f"[red]error:[/red] {message}")


def print_note(message: str) -> None:
    """Print a note to stderr, about what the output on stdout leaves out.

    stderr, so that a note about the 172 results not shown cannot land in the
    middle of the 40 that are about to be piped into something else.
    """

    _console(stderr=True).print(f"[dim]note:[/dim] {message}")


def print_warning(message: str) -> None:
    """Print a warning to stderr, for a command that still succeeded."""

    _console(stderr=True).print(f"[yellow]warning:[/yellow] {message}")
