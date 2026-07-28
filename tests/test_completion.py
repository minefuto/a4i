from __future__ import annotations

import pytest

from a4i import completion
from a4i.cli import build_parser
from a4i.completion import candidates
from a4i.query import QueryTarget, RspPropInclude, RspSubtree, RspSubtreeInclude


@pytest.fixture
def parser():
    return build_parser()


def _complete(parser, line: str, monkeypatch, shell: str = "zsh") -> str:
    """Drive complete() the way the shell widget does, returning what it printed."""

    monkeypatch.setenv(completion.COMPLETE_VAR, shell)
    if shell in {"zsh", "fish"}:
        monkeypatch.setenv(completion.WORDS_VAR, line)
    else:
        words = line.split(" ")
        monkeypatch.setenv("COMP_WORDS", line)
        monkeypatch.setenv("COMP_CWORD", str(len(words) - 1))
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert completion.complete(parser) == 0
    return buf.getvalue().strip()


# -- candidate sources ----------------------------------------------------


def test_enum_flag_value_completion() -> None:
    assert [e.value for e in QueryTarget] == ["self", "children", "subtree"]
    assert [e.value for e in RspSubtree] == ["no", "children", "full"]
    assert [e.value for e in RspPropInclude] == ["all", "naming-only", "config-only"]
    # Categories first, then the modifiers that combine with them.
    assert [e.value for e in RspSubtreeInclude] == [
        "audit-logs",
        "event-logs",
        "faults",
        "fault-records",
        "health",
        "health-records",
        "relations",
        "stats",
        "tasks",
        "count",
        "no-scoped",
        "required",
    ]


def test_fixed_values_are_prefix_matched() -> None:
    complete = completion.complete_from(["faults", "fault-records", "health"])
    assert complete("fault") == ["faults", "fault-records"]
    assert complete("") == ["faults", "fault-records", "health"]
    assert complete("nope") == []


def test_csv_completion_keeps_the_values_already_typed() -> None:
    complete = completion.complete_csv(completion.complete_from(["faults", "health"]))
    assert complete("health,fau") == ["health,faults"]
    # No comma yet: identical to completing a single value.
    assert complete("fau") == ["faults"]


# -- walking the parser ---------------------------------------------------


def test_subcommands_are_completed(parser) -> None:
    assert candidates(parser, [], "") == [
        "login",
        "logout",
        "get",
        "post",
        "list",
        "diff",
        "daemon",
        "mcp",
        "generate-shell-completion",
    ]
    assert candidates(parser, [], "lo") == ["login", "logout"]


def test_shell_argument_is_completed_from_choices(parser) -> None:
    assert candidates(parser, ["generate-shell-completion"], "") == list(completion.SHELLS)
    assert candidates(parser, ["generate-shell-completion"], "f") == ["fish"]


def test_nested_subcommands_are_completed(parser) -> None:
    assert candidates(parser, ["daemon"], "") == ["status", "stop"]
    assert candidates(parser, ["daemon"], "sta") == ["status"]


def test_the_target_kind_is_completed(parser) -> None:
    # What the target names is a subcommand now, so it completes itself from the
    # subparsers' own choices -- no candidate source of its own is involved.
    for command in ("get", "post", "list"):
        assert candidates(parser, [command], "") == ["class", "mo"]
        assert candidates(parser, [command], "m") == ["mo"]


def test_option_names_are_completed(parser) -> None:
    assert candidates(parser, ["get", "mo"], "--rsp") == [
        "--rsp-prop-include",
        "--rsp-subtree",
        "--rsp-subtree-class",
        "--rsp-subtree-filter",
        "--rsp-subtree-include",
    ]
    assert candidates(parser, ["get", "class"], "--rsp") == candidates(
        parser, ["get", "mo"], "--rsp"
    )
    assert "--insecure" in candidates(parser, ["login"], "--")


def test_choices_are_completed(parser) -> None:
    assert candidates(parser, ["get", "mo"], "") == []  # DN: nothing to offer
    assert candidates(parser, ["get", "mo", "--rsp-subtree"], "") == ["no", "children", "full"]
    assert candidates(parser, ["get", "mo", "--rsp-prop-include"], "na") == ["naming-only"]
    assert candidates(parser, ["get", "class", "--query-target"], "s") == ["self", "subtree"]


def test_csv_options_are_completed_after_a_comma(parser) -> None:
    # --rsp-subtree-include carries a type= rather than choices=, because choices
    # would match the whole comma-separated string against one value; its values
    # come from an attached completer instead of the choices fallback.
    assert candidates(parser, ["get", "mo", "--rsp-subtree-include"], "faults,no") == [
        "faults,no-scoped"
    ]
    assert candidates(parser, ["get", "class", "--rsp-subtree-include"], "fau") == [
        "faults",
        "fault-records",
    ]


def test_no_class_or_dn_candidates_anywhere(parser) -> None:
    # Nothing offers class names or DNs any more: "a4i list" does that, where the
    # user asks for the wait rather than paying it on a tab press.
    assert candidates(parser, ["get", "class"], "fvTen") == []
    assert candidates(parser, ["post", "class"], "fvTen") == []
    assert candidates(parser, ["get", "mo"], "uni/tn-") == []
    assert candidates(parser, ["get", "mo", "--target-subtree-class"], "fvB") == []
    assert candidates(parser, ["get", "mo", "--rsp-subtree-class"], "fvB") == []
    assert candidates(parser, ["get", "mo", "--order-by"], "eventRec") == []
    assert candidates(parser, ["list", "class"], "fvTen") == []
    assert candidates(parser, ["list", "mo"], "uni/") == []


def test_no_candidates_once_the_positionals_are_given(parser) -> None:
    assert candidates(parser, ["get", "class", "fvTenant"], "") == []
    assert candidates(parser, ["generate-shell-completion", "zsh"], "") == []


def test_scan_counts_the_positionals_the_options_leave(parser) -> None:
    get_mo, _ = completion._resolve(parser, ["get", "mo"])
    # --node swallows "leaf1", so the DN slot is still waiting; --raw is a flag
    # and shifts nothing.
    assert completion._scan(get_mo, ["--node", "leaf1", "--raw"]) == 0
    assert completion._scan(get_mo, ["--node", "leaf1", "uni/tn-common"]) == 1


def test_unknown_option_is_assumed_to_stand_alone(parser) -> None:
    assert candidates(parser, ["--nope"], "lo") == ["login", "logout"]
    assert candidates(parser, ["get", "--nope"], "m") == ["mo"]


def test_completing_an_option_value_beats_the_positional(parser) -> None:
    # "--node" is the last word, so what is being completed is its value -- a
    # host -- and not the DN that would come next.
    assert candidates(parser, ["get", "mo", "--node"], "") == []


# -- output ---------------------------------------------------------------


def test_zsh_output_uses_compadd_without_rematching(parser, monkeypatch) -> None:
    out = _complete(parser, "a4i get cl", monkeypatch)
    assert out == "compadd -U -- class"


def test_zsh_output_falls_back_to_files_when_empty(parser, monkeypatch) -> None:
    assert _complete(parser, "a4i get mo ", monkeypatch) == "_files"


def test_zsh_output_quotes_values_that_need_it() -> None:
    rendered = completion._render("zsh", ["/uni/tn-t/out-o/instP-i/extsubnet-[10.0.0.0/8]"])
    assert "'/uni/tn-t/out-o/instP-i/extsubnet-[10.0.0.0/8]'" in rendered


def test_bash_output_is_newline_separated(parser, monkeypatch) -> None:
    out = _complete(parser, "a4i get ", monkeypatch, shell="bash")
    assert out.split("\n") == ["class", "mo"]


def test_fish_output_is_newline_separated(parser, monkeypatch) -> None:
    out = _complete(parser, "a4i get ", monkeypatch, shell="fish")
    assert out.split("\n") == ["class", "mo"]


def test_fish_output_falls_back_to_the_files_marker_when_empty(parser, monkeypatch) -> None:
    out = _complete(parser, "a4i get mo ", monkeypatch, shell="fish")
    assert out == completion.FILES_MARKER


def test_trailing_space_starts_a_new_word(parser, monkeypatch) -> None:
    assert _complete(parser, "a4i ", monkeypatch) == (
        "compadd -U -- login logout get post list diff daemon mcp generate-shell-completion"
    )
    assert _complete(parser, "a4i lo", monkeypatch) == "compadd -U -- login logout"


def test_fish_splits_words_like_zsh(parser, monkeypatch) -> None:
    # fish passes the raw line up to the cursor, so the trailing space decides
    # whether a new word has started, exactly as it does for zsh.
    assert _complete(parser, "a4i lo", monkeypatch, shell="fish") == "login\nlogout"
    assert _complete(parser, "a4i daemon ", monkeypatch, shell="fish") == "status\nstop"


def test_unusable_requests_produce_nothing(parser, monkeypatch) -> None:
    monkeypatch.delenv(completion.COMPLETE_VAR, raising=False)
    assert _no_output(parser) == ""
    monkeypatch.setenv(completion.COMPLETE_VAR, "tcsh")
    assert _no_output(parser) == ""
    monkeypatch.setenv(completion.COMPLETE_VAR, "bash")
    monkeypatch.setenv("COMP_WORDS", "a4i get")
    monkeypatch.setenv("COMP_CWORD", "not-a-number")
    assert _no_output(parser) == ""
    monkeypatch.setenv(completion.COMPLETE_VAR, "zsh")
    monkeypatch.delenv(completion.WORDS_VAR, raising=False)
    assert _no_output(parser) == ""


def _no_output(parser) -> str:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert completion.complete(parser) == 0
    return buf.getvalue().strip()


# -- the widget itself ----------------------------------------------------


def test_completion_scripts() -> None:
    zsh = completion.completion_script("zsh")
    assert "#compdef a4i" in zsh
    assert completion.WORDS_VAR in zsh
    # Every widget is plainly synchronous: nothing a tab press asks for reaches
    # the network, so there is no wait left to draw an indicator over.
    assert "zselect" not in zsh
    bash = completion.completion_script("bash")
    assert "complete -o default -F _a4i_completion a4i" in bash
    assert "COMP_CWORD" in bash
    fish = completion.completion_script("fish")
    assert "complete -c a4i -f -a '(_a4i_completion)'" in fish
    assert completion.FILES_MARKER in fish
    assert "__fish_complete_path" in fish
    assert completion.WORDS_VAR in fish
    with pytest.raises(ValueError, match="unsupported shell"):
        completion.completion_script("tcsh")
    # There is no autodetection any more: the shell has to be named.
    with pytest.raises(ValueError, match="unsupported shell"):
        completion.completion_script("")


# -- the CLI surface ------------------------------------------------------


def test_command_help_lists_every_command(parser) -> None:
    help_text = parser.format_help()
    for command in (
        "login",
        "logout",
        "get",
        "post",
        "list",
        "diff",
        "daemon",
        "mcp",
        "generate-shell-completion",
    ):
        assert command in help_text


def test_target_kind_help_lists_both_kinds(parser) -> None:
    commands = completion._subparsers(parser)
    assert commands is not None
    for command in ("get", "post", "list"):
        help_text = commands.choices[command].format_help()
        assert "class" in help_text
        assert "mo" in help_text


@pytest.mark.parametrize("kind", ["class", "mo"])
def test_get_help_lists_every_flag(parser, kind: str) -> None:
    get, _ = completion._resolve(parser, ["get", kind])
    help_text = get.format_help()
    # The option names are the ACI query parameter names verbatim.
    for flag in (
        "--query-target",
        "--target-subtree-class",
        "--query-target-filter",
        "--rsp-subtree",
        "--rsp-subtree-class",
        "--rsp-subtree-filter",
        "--rsp-subtree-include",
        "--rsp-prop-include",
        "--order-by",
        "--page",
        "--page-size",
        "--node",
        "--raw",
    ):
        assert flag in help_text
