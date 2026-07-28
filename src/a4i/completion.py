"""Shell completion, derived from the ``argparse`` parser itself.

Everything on offer comes from the parser: which commands and subcommands exist,
which options exist, which of them take a value, their ``choices``, and where the
positional arguments are. The parser is therefore the single source of truth, and
adding an option to the CLI cannot leave the completion behind.

Nothing here reaches the APIC or the daemon, and nothing loads a dictionary: a
tab press is answered from the parser alone, so it costs one process start and no
I/O. Class names and DNs are listed by ``a4i list`` instead, where the wait is
the user's to ask for. The one candidate source that is not plain ``choices`` is
the comma-separated list, which ``choices`` cannot express.
"""

from __future__ import annotations

import argparse
import os
import shlex
from collections.abc import Callable, Sequence
from typing import Any, Protocol, cast

Completer = Callable[[str], Sequence[str]]

# Set by the shell widget to the shell's name. Its presence is what tells the CLI
# that this run is a completion request rather than a command.
COMPLETE_VAR = "_A4I_COMPLETE"
# zsh and fish: the command line up to the cursor, as one string.
WORDS_VAR = "_A4I_COMPLETE_WORDS"

SHELLS = ("bash", "zsh", "fish")

# fish has no equivalent of zsh's _files fallback, so an empty candidate list has
# to be signalled back to the widget, which then completes paths itself.
FILES_MARKER = "__a4i_files__"


# -- candidate sources ----------------------------------------------------


def complete_from(values: Sequence[str]) -> Completer:
    """Return a completer that prefix-matches a fixed list of values."""

    def complete(incomplete: str) -> list[str]:
        return [value for value in values if value.startswith(incomplete)]

    return complete


def complete_csv(complete_one: Completer) -> Completer:
    """Lift a single-value completer into one for a comma-separated list.

    Only the text after the last comma is a candidate prefix; what came before
    is prepended back, because the shell replaces the whole word.
    """

    def complete(incomplete: str) -> list[str]:
        head, comma, last = incomplete.rpartition(",")
        return [f"{head}{comma}{value}" for value in complete_one(last)]

    return complete


class _Completable(Protocol):
    completer: Completer


def attach(action: argparse.Action, completer: Completer) -> None:
    """Give an argparse action a completion callback.

    argparse has no notion of completion, so the callback rides along on the
    action as an attribute -- the convention argcomplete established -- which
    keeps the parser the one place the CLI is described.
    """

    cast("_Completable", action).completer = completer


# -- request parsing ------------------------------------------------------


def _split(string: str) -> list[str]:
    """Split a command line like ``shlex.split``, tolerating an unfinished token.

    A completion request is by definition mid-word, so an unterminated quote is
    normal rather than an error; the partial token is used as-is.
    """

    lex = shlex.shlex(string, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    words: list[str] = []
    try:
        words.extend(lex)
    except ValueError:
        # End of string reached in an invalid state: the quote or escape is held
        # in lex.state, so lex.token is the partial word.
        words.append(lex.token)
    return words


def _request_args(shell: str) -> tuple[list[str], str] | None:
    """Return ``(words, incomplete)`` for the pending request, or None if unusable."""

    if shell in {"zsh", "fish"}:
        line = os.environ.get(WORDS_VAR)
        if line is None:
            return None
        words = _split(line)[1:]  # drop the program name
        if words and not line.endswith(" "):
            return words[:-1], words[-1]
        return words, ""

    line = os.environ.get("COMP_WORDS")
    raw_index = os.environ.get("COMP_CWORD")
    if line is None or raw_index is None:
        return None
    try:
        index = int(raw_index)
    except ValueError:
        return None
    if index < 1:
        return None
    words = _split(line)
    return words[1:index], words[index] if index < len(words) else ""


# -- walking the parser ---------------------------------------------------


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[Any] | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _takes_value(action: argparse.Action) -> bool:
    """True if the option consumes a following word (as opposed to a plain flag)."""

    return action.nargs != 0


def _option(parser: argparse.ArgumentParser, word: str) -> argparse.Action | None:
    for action in parser._actions:
        if word in action.option_strings:
            return action
    return None


def _resolve(
    parser: argparse.ArgumentParser, words: list[str]
) -> tuple[argparse.ArgumentParser, list[str]]:
    """Follow subcommands in ``words``, returning the innermost parser and the rest."""

    sub = _subparsers(parser)
    if sub is None:
        return parser, words
    for i, word in enumerate(words):
        if word.startswith("-"):
            continue
        child = sub.choices.get(word)
        if child is None:
            break
        return _resolve(child, words[i + 1 :])
    return parser, words


def _positionals(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in parser._actions if not a.option_strings]


def _from_action(action: argparse.Action, incomplete: str) -> list[str]:
    """Candidates for one action: its completer if it has one, else its choices."""

    completer = getattr(action, "completer", None)
    if completer is not None:
        # The completer does its own matching -- a comma-separated list matches
        # on the last element only -- so its result must not be filtered again.
        return list(completer(incomplete))
    if action.choices:
        return [c for c in map(str, action.choices) if c.startswith(incomplete)]
    return []


def _scan(parser: argparse.ArgumentParser, rest: list[str]) -> int:
    """Return how many positional slots ``rest`` fills.

    An option that takes a value swallows the next word, which is therefore not
    a positional, so the options have to be walked to count the positionals.
    """

    used = 0
    i = 0
    while i < len(rest):
        action = _option(parser, rest[i])
        if action is not None:
            i += 2 if _takes_value(action) else 1
        elif rest[i].startswith("-"):
            i += 1  # unknown option; assume it stands alone
        else:
            used += 1
            i += 1
    return used


def candidates(parser: argparse.ArgumentParser, words: list[str], incomplete: str) -> list[str]:
    """Return the completion candidates for ``incomplete`` in the context of ``words``."""

    parser, rest = _resolve(parser, words)

    if incomplete.startswith("-"):
        return sorted(
            opt
            for action in parser._actions
            for opt in action.option_strings
            if opt.startswith(incomplete)
        )

    # A trailing option is still waiting for its value.
    if rest:
        pending = _option(parser, rest[-1])
        if pending is not None and _takes_value(pending):
            return _from_action(pending, incomplete)

    # Otherwise complete the next positional in line, after the ones already given.
    used = _scan(parser, rest)
    slots = _positionals(parser)
    if used < len(slots):
        return _from_action(slots[used], incomplete)
    return []


# -- output ---------------------------------------------------------------


def _render(shell: str, values: list[str]) -> str:
    if shell == "bash":
        return "\n".join(values)
    if shell == "fish":
        return "\n".join(values) if values else FILES_MARKER
    if not values:
        return "_files"
    # -U keeps zsh from matching the candidates against the current word again:
    # the matching already happened in Python, and for a comma-separated list it
    # happened on the last element only. Values are shell-quoted, and not passed
    # through -Q, so that zsh still quotes them on insertion.
    return "compadd -U -- " + " ".join(shlex.quote(v) for v in values)


def complete(parser: argparse.ArgumentParser) -> int:
    """Print candidates for the pending completion request."""

    shell = os.environ.get(COMPLETE_VAR, "")
    if shell not in SHELLS:
        return 0
    parsed = _request_args(shell)
    if parsed is None:
        return 0
    words, incomplete = parsed
    print(_render(shell, candidates(parser, words, incomplete)))
    return 0


# -- the widget itself ----------------------------------------------------

_SCRIPTS = {
    # Every widget is plainly synchronous. Nothing a tab press asks for reaches
    # the network any more, so there is nothing slow enough to need an indicator
    # -- which is just as well, since only zsh could have drawn one without
    # leaving debris on the command line.
    "zsh": f"""\
#compdef a4i
_a4i_completion() {{
  eval "$(env {COMPLETE_VAR}=zsh {WORDS_VAR}="${{words[1,$CURRENT]}}" a4i)"
}}
compdef _a4i_completion a4i
""",
    "bash": f"""\
_a4i_completion() {{
  local IFS=$'\\n'
  COMPREPLY=( $(env {COMPLETE_VAR}=bash COMP_WORDS="${{COMP_WORDS[*]}}" \\
                    COMP_CWORD="$COMP_CWORD" a4i) )
}}
complete -o default -F _a4i_completion a4i
""",
    "fish": f"""\
function _a4i_completion
    # string collect keeps a multi-line edit buffer as one argument.
    set -l line (commandline -cp | string collect)
    set -l out (env {COMPLETE_VAR}=fish {WORDS_VAR}="$line" a4i)
    if test "$out[1]" = "{FILES_MARKER}"
        __fish_complete_path (commandline -ct)
    else
        printf '%s\\n' $out
    end
end
# -f turns off fish's own file completion; the marker above brings it back for
# the arguments that really are paths.
complete -c a4i -f -a '(_a4i_completion)'
""",
}


def completion_script(shell: str) -> str:
    """Return the completion widget for ``shell``."""

    if shell not in SHELLS:
        raise ValueError(f"unsupported shell: {shell} (expected one of {', '.join(SHELLS)})")
    return _SCRIPTS[shell]
