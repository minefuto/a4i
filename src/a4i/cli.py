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
from typing import TYPE_CHECKING, Any

from a4i import completion, query
from a4i.completion import attach, complete_csv, complete_from
from a4i.errors import A4iError, NotLoggedInError, SessionExpiredError
from a4i.ipc import DaemonError, request
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

    Both shapes of the same failure land here: get and post go through a
    transport that restores the typed exception, while login and the daemon
    commands still read the daemon's own classification off a DaemonError.
    """

    from a4i.output import print_error

    stale = isinstance(exc, SessionExpiredError | NotLoggedInError) or (
        isinstance(exc, DaemonError) and exc.type in {"expired", "not_logged_in"}
    )
    hint = " (run 'a4i login')" if stale else ""
    print_error(f"{exc}{hint}")
    return 1


def _warn_apic(info: dict[str, Any]) -> None:
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

    password = os.environ.get("APIC_PASSWORD") or getpass.getpass("APIC password: ")
    verify: bool | str = args.ca if args.ca else not args.insecure
    try:
        info = request(
            "login",
            host=args.host,
            user=args.user,
            password=password,
            verify=verify,
            read_only=args.read_only,
        )
    except DaemonError as exc:
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
        info = request("logout", autostart=False)
    except DaemonError as exc:
        # No daemon means we are already logged out, but an unusable socket is
        # worth saying out loud rather than reporting success we cannot vouch for.
        if exc.type == "socket":
            return _fail(exc)
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
    """Compare the fabric's whole configuration against the intended one."""

    from a4i.output import print_error, render_diff

    try:
        configs = load_configs(args.paths)
    except (OSError, ValueError) as exc:
        print_error(str(exc))
        return 1
    try:
        changes = _client().diff(*configs, expand=args.expand, exclude=args.exclude)
    except ValueError as exc:
        # An MO in the input whose DN cannot be worked out, or an empty
        # --exclude. Comparing the rest would report a fabric that matches on
        # the strength of MOs never looked at, so nothing is printed.
        print_error(str(exc))
        return 1
    except A4iError as exc:
        return _fail(exc)
    render_diff(changes, raw=args.raw)
    # 0 means the fabric matches the configuration, 2 that it differs.
    # Failing to read the fabric at all is raised above and keeps the usual 1.
    return 2 if changes else 0


def load_configs(paths: list[str]) -> list[object]:
    """Read the intended configuration from files, directories and stdin.

    Public because the MCP server's diff tool reads the same paths the command
    does, and a configuration split across a directory has to merge in the same
    order whichever entry point asked for it.

    A directory is walked for ``*.json`` in path order, so that a name can carry
    a numeric prefix; a path named outright is read whatever it is called. The
    reading order is the merge order, so a later file's attributes win.
    """

    import json
    from pathlib import Path

    def parse(text: str, name: str) -> object:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ValueError(f"{name}: invalid JSON: {exc}") from None

    configs: list[object] = []
    for name in paths:
        if name == "-":
            configs.append(parse(sys.stdin.read(), "<stdin>"))
            continue
        path = Path(name)
        if path.is_dir():
            configs.extend(parse(f.read_text(), str(f)) for f in sorted(path.rglob("*.json")))
        else:
            configs.append(parse(path.read_text(), str(path)))
    return configs


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
        data = _client().get(
            args.dn,
            kind="mo",
            query_target="children",
            # Only the DNs are printed, so this asks for the least the APIC can
            # send while still naming each child.
            rsp_prop_include="naming-only",
            node=args.node,
        )
    except A4iError as exc:
        return _fail(exc)
    for dn in _child_dns(data):
        print(dn)
    return 0


def _child_dns(data: Any) -> list[str]:
    """Return the sorted, de-duplicated DNs of the MOs in an ``imdata`` response."""

    dns: set[str] = set()
    for child in data.get("imdata") or []:
        for body in child.values():
            dn = (body.get("attributes") or {}).get("dn")
            if isinstance(dn, str) and dn.strip("/"):
                dns.add(dn.strip("/"))
    return sorted(dns)


def _cmd_daemon_status(args: argparse.Namespace) -> int:
    """Show the daemon's session status."""

    try:
        info = request("status", autostart=False)
    except DaemonError as exc:
        if exc.type == "socket":
            return _fail(exc)
        print("daemon not running")
        return 0
    read_only = " (read-only)" if info.get("read_only") else ""
    if not info.get("logged_in"):
        print(f"daemon running, logged out{read_only}")
        return 0
    print(
        f"logged in to {info['host']} as {info['user']}{read_only}, "
        f"expires in {int(info['expires_in'])}s"
    )
    return 0


def _cmd_daemon_stop(args: argparse.Namespace) -> int:
    """Stop the daemon (ends the session on the APIC and drops the token)."""

    try:
        info = request("stop", autostart=False)
    except DaemonError as exc:
        if exc.type == "socket":
            return _fail(exc)
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

    diff = commands.add_parser(
        "diff",
        help="compare the fabric against an intended configuration",
        description=_cmd_diff.__doc__,
    )
    # No completer: an action with neither one nor choices completes to nothing,
    # which is what tells the shell to fall back on its own file completion.
    diff.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="JSON file, or directory searched for *.json ('-' reads stdin)",
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
    diff.add_argument(
        "--exclude",
        action="append",
        metavar="DN",
        help="leave this MO and everything under it out of the comparison (repeatable)",
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
