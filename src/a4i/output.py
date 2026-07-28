"""Render APIC responses and errors for the terminal.

``rich`` is imported lazily: shell completion goes through ``cli`` but never
renders anything, and importing ``rich.console`` there costs more than the
completion lookup itself.
"""

from __future__ import annotations

import json
import sys
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
    from rich.console import Console

    return Console(stderr=stderr)


def render(data: Any, *, raw: bool = False) -> None:
    """Print ``data`` as JSON: colorized on a TTY, plain otherwise or with raw."""

    if raw or not sys.stdout.isatty():
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        _console().print_json(data=data)


def render_dry_run(changes: list[Change], *, raw: bool = False) -> None:
    """Print the changes a POST would cause: colorized on a TTY, plain otherwise."""

    _print(_report(changes, _DRY_RUN_KINDS, "no changes"), raw=raw)


def render_diff(changes: list[Change], *, raw: bool = False) -> None:
    """Print how the fabric differs from its intended configuration."""

    _print(_report(changes, _DIFF_KINDS, "no differences"), raw=raw)


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


def _print(lines: list[str], *, raw: bool) -> None:
    if raw or not sys.stdout.isatty():
        for line in lines:
            print(line)
        return
    console = _console()
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
        header += f"  ({note.format(_plural(change.child_count, 'child MO'))})"
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
        parts.append(_plural(counts["warning"], "warning"))
    return ", ".join(parts)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _quote(value: str | None) -> str:
    return json.dumps(value, ensure_ascii=False)


def print_error(message: str) -> None:
    """Print an error message to stderr."""

    _console(stderr=True).print(f"[red]error:[/red] {message}")


def print_warning(message: str) -> None:
    """Print a warning to stderr, for a command that still succeeded."""

    _console(stderr=True).print(f"[yellow]warning:[/yellow] {message}")
