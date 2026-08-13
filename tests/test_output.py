"""How the bundled model is laid out, at the width it is being read at.

These run against a4i.output directly, over a console opened on a string. That
is what lets a width be named: the layout decides what to cut and what to drop
from how wide the terminal is, and a test that went through the command line
would be reading a pipe, which has no width to overflow.

They run against the dictionary that ships with a4i rather than against records
written here, because the numbers this lays out -- thirty properties, thirty-four
children, forty parents -- are what the layout exists to cope with.

What the commands do with all this -- which renderer they reach for, what they
say on stderr, what they return -- is tests/test_cli.py's.
"""

from __future__ import annotations

import io
from typing import cast

import pytest
from rich.console import Console

from a4i import metadata, output


def _console(width: int | None = None) -> Console:
    """A console over a string: a terminal of ``width``, or a pipe with none."""

    file = io.StringIO()
    if width is None:
        # is_terminal is False, so nothing is cut and nothing is styled.
        return Console(file=file, force_terminal=False)
    return Console(file=file, force_terminal=True, width=width, no_color=True)


def _rendered(console: Console) -> str:
    # Always the StringIO _console wrote the console over.
    return cast("io.StringIO", console.file).getvalue()


def _search(keyword: str, limit: int, width: int | None = None, raw: bool = False) -> str:
    console = _console(width)
    output.render_search(metadata.search(keyword, limit=limit), raw=raw, console=console)
    return _rendered(console)


def _describe(class_name: str, width: int | None = None, raw: bool = False, **kwargs) -> str:
    console = _console(width)
    record = metadata.describe(class_name)
    assert record is not None
    output.render_describe(record, raw=raw, console=console, **kwargs)
    return _rendered(console)


# A width to lay out at, with the styling off. It is what --raw on a terminal
# gives, and it is the state a layout can be read back from exactly: the escape
# codes a styled line carries are not columns, and counting them as columns is
# how you come to believe a line fits when it does not.
SIZED = {"raw": True}


# -- search ------------------------------------------------------------------


def test_the_class_name_opens_every_line() -> None:
    lines = _search("bridge domain", 5).splitlines()
    # The label is what matched, and fvBD is the bridge domain rather than one
    # of the two hundred relations pointing at it.
    assert lines[0].split()[0] == "fvBD"
    assert all(line.split()[0].isalnum() for line in lines)


def test_a_line_is_cut_to_the_terminal_width() -> None:
    line = _search("bridge domain", 1, width=60, **SIZED).splitlines()[0]
    assert len(line) == 60
    assert line.endswith("…")


def test_the_summary_is_dropped_when_it_cannot_be_fitted() -> None:
    # Below twenty columns what is left of a summary is a fragment, and a column
    # of fragments is worse than a table of names and labels.
    assert _search("bridge domain", 1, width=32, **SIZED).splitlines()[0] == "fvBD  Bridge Domain"


def test_a_piped_summary_is_left_whole() -> None:
    # Nothing is watching, so nothing is cut: what a person would have seen
    # truncated still reaches grep entire.
    assert "…" not in _search("bridge domain", 1)


def test_a_terminal_styles_the_class_name_and_raw_takes_it_away() -> None:
    """The two questions the console is asked, and why they are not one.

    --raw asks for the styling to go. It does not ask for the lines to stop
    fitting the terminal they are still being read in: both of these are cut to
    sixty, and only one of them is styled.
    """

    styled = _search("bridge domain", 1, width=60).splitlines()[0]
    plain = _search("bridge domain", 1, width=60, raw=True).splitlines()[0]
    assert "\x1b[" in styled and "\x1b[" not in plain
    assert styled.endswith("…") and plain.endswith("…")
    # The escapes are not columns: the styled line is longer in bytes and the
    # same width on screen.
    assert len(plain) == 60
    assert len(styled) > 60


# -- describe ----------------------------------------------------------------


def test_what_a_body_may_set_is_shown() -> None:
    out = _describe("fvBD")
    assert out.startswith("fvBD  Bridge Domain")
    assert "rn  BD-{name}" in out
    assert "dn  uni/tn-{name}/BD-{name}" in out
    assert "in  fvTenant" in out


def test_the_naming_property_is_marked() -> None:
    # The star is what says which property the RN is built from.
    assert "name*" in _describe("fvBD")


def test_the_permitted_values_and_the_default_are_given() -> None:
    out = _describe("fvBD")
    assert "flood|proxy = proxy" in out
    # No permitted values, so the length a string may be is what a body gets
    # wrong; the type alone would not have said it.
    assert "string:Basic (1-64)" in out


def test_a_narrow_terminal_keeps_the_values_and_drops_the_prose() -> None:
    # Permitted values are what a body gets wrong; a description only helps you
    # guess which property you wanted. So the description is what gives way.
    out = _describe("fvBD", width=76, **SIZED)
    assert "  arpFlood                  no|yes = no\n" in out
    assert "A property to specify" not in out


def test_a_bound_nothing_can_satisfy_is_not_printed() -> None:
    # fvCtx.vrfIndex is bounded 1 to 0 in the MIM, which describes no value at
    # all. The JSON still carries it -- see tests/test_cli.py.
    assert "vrfIndex             scalar:Uint32 = 0" in _describe("fvCtx")


def test_the_read_only_properties_are_hidden_but_counted() -> None:
    out = _describe("fvBD")
    assert "read-only hidden" in out
    assert "bcastP" not in out


def test_all_spells_the_read_only_properties_out() -> None:
    out = _describe("fvBD", all_props=True)
    assert "read-only properties (" in out
    assert "bcastP" in out


def test_the_children_are_counted_and_listed_on_request() -> None:
    counted = _describe("fvBD")
    assert "children (34)" in counted
    assert "fvSubnet" not in counted
    assert "fvSubnet" in _describe("fvBD", children=True)


def test_a_class_that_cannot_be_configured_says_so() -> None:
    # Most of the dictionary is classes like this, so the absent rn line has to
    # read as "this cannot be created" rather than as something left out.
    out = _describe("aaaAppToken")
    assert "(read-only class)" in out
    assert "\nrn  " not in out
    # Every property is read-only here, so hiding them would leave a heading
    # with nothing under it.
    assert "all read-only" in out
    assert "appName" in out


def test_every_dn_a_class_is_reachable_by_is_reported() -> None:
    assert _describe("fvRsCtx").count("\ndn  ") == 3


def test_the_parents_the_dictionary_dropped_are_counted() -> None:
    # The generator stops at eight, and a class contained by forty must not read
    # as one contained by eight.
    assert "and 33 more" in _describe("aaaADomainRefTask")


# -- what a console decides --------------------------------------------------


@pytest.mark.parametrize("raw", [False, True])
def test_a_pipe_is_never_styled_however_raw_is_left(raw) -> None:
    # is_terminal answers this on its own; --raw has nothing left to settle.
    assert "\x1b[" not in _search("bridge domain", 3, raw=raw)


def test_json_is_written_past_richs_wrapping() -> None:
    """A wrapped line is a JSON document broken in half.

    rich wraps to the console's width, which for anything but a terminal is a
    default 80. The plain path writes to the console's file instead, so a long
    value survives whatever the width happens to be.
    """

    console = _console()
    long_dn = "uni/tn-" + "x" * 200
    output.render({"imdata": [{"fvTenant": {"attributes": {"dn": long_dn}}}]}, console=console)
    assert long_dn in _rendered(console)
    assert max(len(line) for line in _rendered(console).splitlines()) > 80
