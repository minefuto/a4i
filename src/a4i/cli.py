"""argparse command-line interface for the ACI REST API.

Typer is deliberately not used here. Shell completion spawns a whole new process
on every tab press, and importing Typer cost more than the completion lookup it
enabled. argparse costs a fraction of that, and doubles as the single source of
truth that :mod:`a4i.completion` walks to decide what to offer.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

from a4i import completion, ipc, query
from a4i.completion import attach, complete_csv, complete_from
from a4i.errors import (
    A4iError,
    DaemonError,
    NotLoggedInError,
    SessionExpiredError,
    UnusableSocketError,
)
from a4i.query import QueryTarget, RspPropInclude, RspSubtree, RspSubtreeInclude

if TYPE_CHECKING:
    from a4i.client import Client

# The option names are the ACI query parameter names verbatim, so that a
# parameter read in the APIC documentation can be typed as-is. The enums that
# spell out their values live in a4i.query, next to the code that maps an option
# to the parameter it sets, so the library validates against the same list.


def _csv_choices(enum: type[StrEnum]) -> Callable[[str], str]:
    """Return an argparse type validating a comma-separated list of enum values.

    ``choices`` cannot do this: it would match the whole comma-separated string
    against a single value.
    """

    allowed = query.values(enum)

    def parse(value: str) -> str:
        items = [item.strip() for item in value.split(",")]
        unknown = [item for item in items if item not in allowed]
        if unknown:
            raise argparse.ArgumentTypeError(
                f"invalid value: {', '.join(unknown)} (choose from {', '.join(allowed)})"
            )
        return ",".join(items)

    return parse


def _bounded_int(minimum: int) -> Callable[[str], int]:
    """Return an argparse type accepting an integer no smaller than ``minimum``."""

    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
        if number < minimum:
            raise argparse.ArgumentTypeError(f"must be {minimum} or greater, not {number}")
        return number

    return parse


def _positive_float(value: str) -> float:
    """Return ``value`` as a number of seconds a request can be given.

    Zero and below are refused here rather than left to the session, which would
    only reject them after a daemon had been started to hear it -- and with a
    ValueError, which is not one of the failures a command knows how to report.
    """

    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0, not {number}")
    return number


class _VersionAction(argparse.Action):
    """Print the version and exit, reading it only when the flag is given.

    argparse's own ``version`` action wants the string when the parser is built,
    and the parser is built on every tab press; resolving it there would pay for
    importlib.metadata on a completion that never shows a version.
    """

    def __init__(
        self, option_strings: list[str], dest: str = argparse.SUPPRESS, help: str | None = None
    ) -> None:
        super().__init__(option_strings, dest, nargs=0, help=help)

    def __call__(self, parser: argparse.ArgumentParser, *args: object, **kwargs: object) -> None:
        import a4i

        print(a4i.__version__)
        parser.exit()


def _fail(exc: A4iError) -> int:
    """Report a failed request and return the exit code for it.

    Every request answers in the same exceptions, whichever side of the socket
    it was served from, so there is one shape of failure to read here.
    """

    from a4i.output import print_error

    hint = " (run 'a4i login')" if isinstance(exc, SessionExpiredError | NotLoggedInError) else ""
    print_error(f"{exc}{hint}")
    return 1


def _warn_apic(info: ipc.EndReply) -> None:
    """Report an APIC that could not be told the session had ended.

    The token is gone from the daemon either way, so this is a warning over a
    logout that succeeded, not a failure: only the APIC's own copy is left to
    expire on its own.
    """

    message = info.get("apic_error")
    if message:
        from a4i.output import print_warning

        print_warning(f"APIC not notified: {message}")


def _client() -> Client:
    """Build the client a get, a post or a diff runs on, talking through the daemon.

    It is the very same client a library caller builds; only the transport under
    it differs, so a command and a script send the identical request.
    """

    # Imported here rather than at module scope: shell completion reaches this
    # module on every tab press but never issues a request.
    from a4i.client import Client
    from a4i.transport import DaemonTransport

    return Client(transport=DaemonTransport())


# -- commands -------------------------------------------------------------


def _cmd_login(args: argparse.Namespace) -> int:
    """Authenticate to an APIC and cache the token in the daemon's memory."""

    # Imported here rather than at module scope: the default is the session's own
    # and is worth reading from there, but reaching it costs httpx2, and this
    # module is walked by shell completion on every tab press.
    from a4i.session import DEFAULT_TIMEOUT

    password = os.environ.get("APIC_PASSWORD") or getpass.getpass("APIC password: ")
    verify: bool | str = args.ca if args.ca else not args.insecure
    timeout = DEFAULT_TIMEOUT if args.timeout is None else args.timeout
    try:
        info = ipc.login(
            args.host,
            args.user,
            password,
            verify=verify,
            timeout=timeout,
            read_only=args.read_only,
        )
    except A4iError as exc:
        return _fail(exc)
    read_only = info.get("read_only")
    suffix = " (read-only)" if read_only else ""
    print(f"logged in to {info['host']} as {info['user']}{suffix}")
    if read_only and not args.read_only:
        # The daemon was already read-only and a login does not clear it, so this
        # login got less than it asked for. Saying nothing would leave the first
        # refused post to explain it.
        from a4i.output import print_warning

        print_warning(
            "this daemon was started read-only; run 'a4i daemon stop' and log in again to write"
        )
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:
    """End the session on the APIC and drop the in-memory token."""

    try:
        info = ipc.logout()
    except UnusableSocketError as exc:
        # No daemon means we are already logged out, but an unusable socket is
        # worth saying out loud rather than reporting success we cannot vouch for.
        return _fail(exc)
    except DaemonError:
        pass
    else:
        _warn_apic(info)
    print("logged out")
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    """GET the objects of a class, or one MO by its DN."""

    from a4i.output import print_error, render

    # Every option is named after the parameter it sets, on both sides, so this
    # is a rename from dashes to underscores and nothing more.
    try:
        data = _client().get(
            args.target,
            kind=args.kind,
            query_target=args.query_target,
            target_subtree_class=args.target_subtree_class,
            query_target_filter=args.query_target_filter,
            rsp_subtree=args.rsp_subtree,
            rsp_subtree_class=args.rsp_subtree_class,
            rsp_subtree_filter=args.rsp_subtree_filter,
            rsp_subtree_include=args.rsp_subtree_include,
            rsp_prop_include=args.rsp_prop_include,
            order_by=args.order_by,
            page=args.page,
            page_size=args.page_size,
            node=args.node,
        )
    except ValueError as exc:
        # A combination argparse cannot express on its own, such as --page
        # without --page-size.
        print_error(str(exc))
        return 1
    except A4iError as exc:
        return _fail(exc)
    render(data, raw=args.raw)
    return 0


def _cmd_post(args: argparse.Namespace) -> int:
    """POST a JSON body to a class or an MO (body from argument or stdin)."""

    from a4i.output import print_error, render

    body = args.body if args.body is not None else sys.stdin.read()
    client = _client()
    try:
        if args.dry_run:
            return _post_dry_run(args, client, body)
        data = client.post(args.target, body, kind=args.kind)
    except ValueError as exc:
        # An empty or unparseable body, caught before it reaches the daemon.
        print_error(str(exc))
        return 1
    except A4iError as exc:
        return _fail(exc)
    render(data, raw=args.raw)
    return 0


def _post_dry_run(args: argparse.Namespace, client: Client, body: str) -> int:
    """Show what the POST would change, without sending it.

    The APIC has no server-side dry run, so the current state is fetched and the
    comparison happens here. This path never reaches the post op.
    """

    from a4i.output import render_dry_run

    changes = client.dry_run(args.target, body, kind=args.kind)
    render_dry_run(changes, raw=args.raw)
    # 0 means posting this body would do nothing at all; 2 means it would change
    # something or fail. Errors are raised to _cmd_post, which keeps the usual 1.
    return 2 if changes else 0


def _cmd_diff(args: argparse.Namespace) -> int:
    """Compare the fabric against one intended configuration (body or stdin).

    One body, as post takes one body. A configuration written across several
    files is folded into that one body first with 'a4i merge'.
    """

    from a4i.output import print_error, render_diff

    config = args.body if args.body is not None else sys.stdin.read()
    try:
        changes = _client().diff(config, expand=args.expand, exclude=args.exclude)
    except ValueError as exc:
        # An MO in the input whose DN cannot be worked out, or an --exclude that
        # is empty or holds "**". Comparing the rest would report a fabric that
        # matches on the strength of MOs never looked at, so nothing is printed.
        print_error(str(exc))
        return 1
    except A4iError as exc:
        return _fail(exc)
    render_diff(changes, raw=args.raw)
    # 0 means the fabric matches the configuration, 2 that it differs.
    # Failing to read the fabric at all is raised above and keeps the usual 1.
    return 2 if changes else 0


def _cmd_merge(args: argparse.Namespace) -> int:
    """Fold several configuration files into the one body they describe.

    The output is a polUni carrying every merged MO with its own dn, which is
    what 'a4i diff' compares against and what 'a4i post mo uni' takes.
    """

    import json

    from a4i import config
    from a4i.merge import merge
    from a4i.output import print_error

    try:
        configs = config.load(args.paths)
        body = merge(*configs)
    except (OSError, ValueError) as exc:
        print_error(str(exc))
        return 1
    text = json.dumps(body, indent=2, ensure_ascii=False)
    if args.output is None:
        print(text)
        return 0
    try:
        config.write(args.output, text, overwrite=args.force)
    except FileExistsError as exc:
        # Before the OSError below, which it is one of. The rule is the config
        # module's; the way out is this parser's option, so it is named here.
        print_error(f"{exc} (pass --force to overwrite it)")
        return 1
    except OSError as exc:
        print_error(str(exc))
        return 1
    return 0


def _cmd_list_class(args: argparse.Namespace) -> int:
    """List ACI class names from the bundled dictionary, one per line.

    Nothing is sent anywhere: the dictionary ships with a4i, so this answers
    logged out and without a daemon.
    """

    # Imported here rather than at module scope: this module is reached on every
    # tab press, which no longer has any use for the dictionary.
    from a4i.metadata import class_names_startingwith

    for name in class_names_startingwith(args.prefix):
        print(name)
    return 0


def _cmd_list_mo(args: argparse.Namespace) -> int:
    """List the DNs of the MOs directly under DN, one per line.

    The DNs come back bare, so a line of this output is what 'a4i get mo' takes
    as its argument.
    """

    try:
        dns = _client().list_children(args.dn, node=args.node)
    except A4iError as exc:
        return _fail(exc)
    for dn in dns:
        print(dn)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    """Find ACI classes by what they are called, when the name is not known.

    The match is a case-insensitive substring of the class name, its label or
    its one-line summary, so 'bridge domain' finds fvBD. This is the other
    question from 'a4i list class', which matches a prefix of the name and
    nothing else. Nothing is sent anywhere: the dictionary ships with a4i.
    """

    from a4i.metadata import search
    from a4i.output import print_error, print_note, render, render_search

    # The limit is applied last, after the whole dictionary has been walked, so
    # asking for everything is what makes the total knowable -- and the total is
    # the entire point of saying that 40 of 212 were shown.
    results = search(args.keyword, limit=sys.maxsize)
    if not results:
        print_error(f"no classes match {args.keyword!r}")
        return 1
    shown = results[: args.limit] if args.limit else results
    if args.json:
        render(
            [{"class": name, "label": label, "summary": summary} for name, label, summary in shown],
            raw=args.raw,
        )
    else:
        render_search(shown, raw=args.raw)
    if len(shown) < len(results):
        print_note(
            f"{len(results)} matches, showing {len(shown)} -- "
            "'--limit N' for more, '--limit 0' for all"
        )
    return 0


def _cmd_describe(args: argparse.Namespace) -> int:
    """Describe one ACI class from the bundled model.

    What it is for, the properties a body may set with their permitted values
    and defaults, how its RN is built, and which classes contain it. Read-only
    properties and the classes that may hang under it are counted rather than
    listed; --all and --children spell each of them out. Needs no session.
    """

    from a4i.metadata import describe, search
    from a4i.output import print_error, render, render_describe

    record = describe(args.class_name)
    if record is None:
        # Case is the trap here -- ACI class names are case-sensitive and fvbd is
        # not fvBD -- and a dictionary older than the fabric is the other one, so
        # the message names both rather than reporting a bare miss.
        near = search(args.class_name, limit=5)
        hint = "\nDid you mean: " + ", ".join(name for name, _, _ in near) if near else ""
        print_error(
            f"the bundled model does not carry {args.class_name!r}. Class names are "
            "case-sensitive, and the fabric may be newer than the dictionary; you can "
            f"still query the class either way.{hint}"
        )
        return 1
    if args.json:
        render(record, raw=args.raw)
        return 0
    render_describe(record, all_props=args.all, children=args.children, raw=args.raw)
    return 0


def _cmd_daemon_status(args: argparse.Namespace) -> int:
    """Show the daemon's session status."""

    try:
        info = ipc.status()
    except UnusableSocketError as exc:
        return _fail(exc)
    except DaemonError:
        print("daemon not running")
        return 0
    read_only = " (read-only)" if info["read_only"] else ""
    # Read as the key it is rather than with .get, so that what the daemon
    # answered narrows to one shape and the fields below follow from it.
    if not info["logged_in"]:
        print(f"daemon running, logged out{read_only}")
        return 0
    # "request timeout" spelled out rather than left as "timeout": the line
    # already carries an "expires in" that comes from the token's lifetime, and
    # two bare numbers of seconds would read as one thing said twice.
    print(
        f"logged in to {info['host']} as {info['user']}{read_only}, "
        f"expires in {int(info['expires_in'])}s, "
        f"request timeout {info['timeout']:g}s"
    )
    return 0


def _cmd_daemon_stop(args: argparse.Namespace) -> int:
    """Stop the daemon (ends the session on the APIC and drops the token)."""

    try:
        info = ipc.stop()
    except UnusableSocketError as exc:
        return _fail(exc)
    except DaemonError:
        pass
    else:
        _warn_apic(info)
    print("daemon stopped")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the Model Context Protocol on stdin/stdout, for an LLM client.

    Requests go to the same daemon a command uses, so a model reads and writes
    the fabric through the session already logged in -- and through nothing else:
    no port is opened and this process holds no token of its own. It starts
    whether or not anyone is logged in yet, since an MCP client launches it when
    the editor starts.
    """

    from a4i.mcp import serve

    return serve()


def _cmd_generate_shell_completion(args: argparse.Namespace) -> int:
    """Print the completion script to eval from a shell startup file."""

    print(completion.completion_script(args.shell), end="")
    return 0


# -- parser ---------------------------------------------------------------


def _add_query_options(parser: argparse.ArgumentParser) -> None:
    """Add the query options to a ``get`` subcommand.

    ``get class`` and ``get mo`` differ only in what their target names, so the
    options they share are declared once here and attached to both.
    """

    # Scoping filters: what the query walks over.
    parser.add_argument(
        "--query-target",
        choices=query.values(QueryTarget),
        help="scope of the query (default: self)",
    )
    parser.add_argument(
        "--target-subtree-class",
        metavar="CLASS[,CLASS...]",
        help="limit the scope to these classes",
    )
    parser.add_argument(
        "--query-target-filter", metavar="FILTER", help='filter the scope, e.g. eq(fvBD.name,"a")'
    )
    # Response subtree filters: what comes back for each MO in scope.
    parser.add_argument(
        "--rsp-subtree",
        choices=query.values(RspSubtree),
        help="how much of each MO's subtree to return",
    )
    parser.add_argument(
        "--rsp-subtree-class",
        metavar="CLASS[,CLASS...]",
        help="limit the returned subtree to these classes",
    )
    parser.add_argument(
        "--rsp-subtree-filter", metavar="FILTER", help="limit the returned subtree by filter"
    )
    attach(
        parser.add_argument(
            "--rsp-subtree-include",
            metavar="CATEGORY[,CATEGORY...]",
            type=_csv_choices(RspSubtreeInclude),
            help=f"extra subtree categories: {', '.join(query.values(RspSubtreeInclude))}",
        ),
        complete_csv(complete_from(query.values(RspSubtreeInclude))),
    )
    parser.add_argument(
        "--rsp-prop-include",
        choices=query.values(RspPropInclude),
        help="which properties to return",
    )
    # Sorting and pagination.
    parser.add_argument(
        "--order-by",
        metavar="CLASS.PROPERTY[|asc|desc]",
        help="sort the response, e.g. eventRecord.created|desc",
    )
    parser.add_argument(
        "--page", metavar="N", type=_bounded_int(0), help="page to return, counting from 0"
    )
    parser.add_argument("--page-size", metavar="N", type=_bounded_int(1), help="objects per page")
    parser.add_argument(
        "--node",
        metavar="HOST",
        help="query a fabric switch directly (IP or hostname) with the same token",
    )
    parser.add_argument("--raw", action="store_true", help="uncolored JSON output")


def _add_post_options(parser: argparse.ArgumentParser) -> None:
    """Add the body and the flags to a ``post`` subcommand."""

    parser.add_argument("body", nargs="?", help="JSON body; read from stdin if omitted")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what the POST would change instead of sending it",
    )
    # Not "uncolored JSON output" as on get: --dry-run prints a change report,
    # not JSON.
    parser.add_argument("--raw", action="store_true", help="uncolored output")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser, which also serves as the completion spec."""

    parser = argparse.ArgumentParser(
        prog="a4i",
        description="CLI for the Cisco ACI (APIC) REST API. Token is held in memory by a daemon.",
    )
    parser.add_argument("--version", action=_VersionAction, help="print the version and exit")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    login = commands.add_parser(
        "login", help="authenticate to an APIC", description=_cmd_login.__doc__
    )
    login.add_argument("host", help="APIC host, e.g. apic1.example.com")
    login.add_argument("-u", "--user", required=True, help="APIC username")
    login.add_argument("-k", "--insecure", action="store_true", help="skip TLS verification")
    login.add_argument("--ca", help="path to a CA bundle for TLS verification")
    login.add_argument(
        "--timeout",
        type=_positive_float,
        help="seconds a request to the APIC may take before it is given up on (default: 30)",
    )
    login.add_argument(
        "--read-only",
        action="store_true",
        help="refuse every POST for this daemon's life ('a4i daemon stop' to clear)",
    )
    login.set_defaults(func=_cmd_login)

    logout = commands.add_parser(
        "logout", help="drop the in-memory session", description=_cmd_logout.__doc__
    )
    logout.set_defaults(func=_cmd_logout)

    # What the target names is said outright rather than read off its shape, so
    # each of get and post carries the same pair of subcommands. The subparsers'
    # dest is the very argument the client takes, so the word the user typed is
    # what travels down; no handler has to map it.
    get = commands.add_parser("get", help="GET a class or an MO", description=_cmd_get.__doc__)
    # A bare "a4i get" has no target of its own; main() shows this parser's help.
    get.set_defaults(help_parser=get)
    get_kinds = get.add_subparsers(dest="kind")
    get_class = get_kinds.add_parser(
        "class", help="GET every MO of a class", description="GET every MO of an ACI class."
    )
    get_class.add_argument("target", metavar="CLASS", help="ACI class name, e.g. fvTenant")
    _add_query_options(get_class)
    get_class.set_defaults(func=_cmd_get)
    get_mo = get_kinds.add_parser(
        "mo", help="GET one MO by its DN", description="GET one MO by its distinguished name."
    )
    get_mo.add_argument("target", metavar="DN", help="distinguished name, e.g. uni/tn-common")
    _add_query_options(get_mo)
    get_mo.set_defaults(func=_cmd_get)

    post = commands.add_parser("post", help="POST a JSON body", description=_cmd_post.__doc__)
    post.set_defaults(help_parser=post)
    post_kinds = post.add_subparsers(dest="kind")
    post_class = post_kinds.add_parser(
        "class", help="POST to a class", description="POST a JSON body to an ACI class."
    )
    post_class.add_argument("target", metavar="CLASS", help="ACI class name, e.g. fvTenant")
    _add_post_options(post_class)
    post_class.set_defaults(func=_cmd_post)
    post_mo = post_kinds.add_parser(
        "mo", help="POST to an MO", description="POST a JSON body to an MO by its DN."
    )
    post_mo.add_argument("target", metavar="DN", help="distinguished name, e.g. uni/tn-common")
    _add_post_options(post_mo)
    post_mo.set_defaults(func=_cmd_post)

    listing = commands.add_parser("list", help="list class names or child MOs")
    listing.set_defaults(help_parser=listing)
    list_kinds = listing.add_subparsers(dest="kind")
    list_class = list_kinds.add_parser(
        "class", help="list ACI class names", description=_cmd_list_class.__doc__
    )
    list_class.add_argument(
        "prefix",
        nargs="?",
        default="",
        help="only names starting with this, matched case-insensitively",
    )
    list_class.set_defaults(func=_cmd_list_class)
    list_mo = list_kinds.add_parser(
        "mo", help="list the child MOs of a DN", description=_cmd_list_mo.__doc__
    )
    list_mo.add_argument(
        "dn",
        nargs="?",
        default="uni",
        help="parent DN whose children to list (default: uni; a switch's root is sys)",
    )
    list_mo.add_argument(
        "--node",
        metavar="HOST",
        help="list from a fabric switch directly (IP or hostname) with the same token",
    )
    list_mo.set_defaults(func=_cmd_list_mo)

    # search and describe sit next to list because all three read the bundled
    # dictionary and none of them touches the fabric. Neither takes a class/mo
    # kind: the dictionary holds classes, and a level of subcommand with one
    # choice is a word typed for nothing.
    searching = commands.add_parser(
        "search", help="find a class by what it is called", description=_cmd_search.__doc__
    )
    searching.add_argument("keyword", help="words to look for, e.g. 'bridge domain'")
    searching.add_argument(
        "--limit",
        metavar="N",
        type=_bounded_int(0),
        default=40,
        help="most results to show, 0 for all (default: 40)",
    )
    searching.add_argument("--json", action="store_true", help="print the results as JSON")
    # Not "uncolored JSON output" as on get: this prints a table, not JSON.
    searching.add_argument("--raw", action="store_true", help="uncolored output")
    searching.set_defaults(func=_cmd_search)

    describing = commands.add_parser(
        "describe",
        help="describe one class from the bundled model",
        description=_cmd_describe.__doc__,
    )
    # No completer: completion answers from the parser alone and loads no
    # dictionary, which is what keeps a tab press to one process start and no
    # I/O. 'a4i search' and 'a4i list class' are how a class name is found.
    describing.add_argument(
        "class_name", metavar="CLASS", help="exact ACI class name, case-sensitive, e.g. fvBD"
    )
    describing.add_argument(
        "-a", "--all", action="store_true", help="show the read-only properties too"
    )
    describing.add_argument(
        "--children", action="store_true", help="list the classes that may hang under this one"
    )
    describing.add_argument("--json", action="store_true", help="print the bundled record as JSON")
    describing.add_argument("--raw", action="store_true", help="uncolored output")
    describing.set_defaults(func=_cmd_describe)

    merge = commands.add_parser(
        "merge",
        help="fold several configuration files into one body",
        description=_cmd_merge.__doc__,
    )
    # No completer: an action with neither one nor choices completes to nothing,
    # which is what tells the shell to fall back on its own file completion.
    merge.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="JSON file, or directory searched for *.json ('-' reads stdin)",
    )
    merge.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="write the merged body here instead of stdout",
    )
    merge.add_argument(
        "--force", action="store_true", help="overwrite the output file if it exists"
    )
    merge.set_defaults(func=_cmd_merge)

    diff = commands.add_parser(
        "diff",
        help="compare the fabric against an intended configuration",
        description=_cmd_diff.__doc__,
    )
    diff.add_argument(
        "body",
        nargs="?",
        help="the intended configuration as a JSON body; read from stdin if omitted",
    )
    diff.add_argument(
        "--expand",
        action="store_true",
        help="list every MO instead of summarising a wholly missing or extra subtree",
    )
    # Repeatable rather than comma-separated, as the class list options are: an
    # ACI naming value can hold a comma, so a DN cannot be split on one. No
    # completer, for the reason the paths argument has none -- and DNs live on
    # the fabric, which a tab press never reaches.
    # Quote the value: "*" is the shell's before it is ours.
    diff.add_argument(
        "--exclude",
        action="append",
        metavar="PATTERN",
        help=(
            "leave this MO and everything under it out of the comparison: a DN, or a "
            "quoted pattern whose '*' matches within one RN, as in 'uni/tn-test*' "
            "(repeatable)"
        ),
    )
    # Not "uncolored JSON output" as on get: diff prints a report, not JSON.
    diff.add_argument("--raw", action="store_true", help="uncolored output")
    diff.set_defaults(func=_cmd_diff)

    daemon = commands.add_parser("daemon", help="manage the background token daemon")
    # A bare "a4i daemon" has no action of its own; main() shows this parser's help.
    daemon.set_defaults(help_parser=daemon)
    daemon_commands = daemon.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    status = daemon_commands.add_parser(
        "status", help="show session status", description=_cmd_daemon_status.__doc__
    )
    status.set_defaults(func=_cmd_daemon_status)
    stop = daemon_commands.add_parser(
        "stop", help="stop the daemon", description=_cmd_daemon_stop.__doc__
    )
    stop.set_defaults(func=_cmd_daemon_stop)

    mcp = commands.add_parser(
        "mcp",
        help="serve MCP on stdin/stdout for an LLM client",
        description=_cmd_mcp.__doc__,
    )
    mcp.set_defaults(func=_cmd_mcp)

    generate = commands.add_parser(
        "generate-shell-completion",
        help="print the shell completion script",
        description=_cmd_generate_shell_completion.__doc__,
    )
    # choices is enough for the SHELL argument to complete itself: _from_action
    # falls back to them when an action has no completer of its own.
    generate.add_argument("shell", choices=completion.SHELLS, help="shell to generate for")
    generate.set_defaults(func=_cmd_generate_shell_completion)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # A completion request must not pay for argument parsing or output rendering.
    if os.environ.get(completion.COMPLETE_VAR):
        return completion.complete(parser)
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        # No command at all, or a command group given without its subcommand.
        getattr(args, "help_parser", parser).print_help()
        return 0
    return args.func(args)
