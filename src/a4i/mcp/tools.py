"""The tools the MCP server offers, and what each one does.

Every tool here is a thin wrapper over the very same :class:`~a4i.client.Client`
the command line drives, so a model and a person send the identical request. What
is added is the part a model needs and a person does not: a schema saying what
the arguments are, a size limit so that one query cannot fill a context window,
and errors phrased as instructions rather than as diagnostics.

The argument names are the CLI's option names with underscores, which are the
ACI query parameter names themselves. A model that has read the APIC
documentation can therefore write a query without learning anything a4i-specific.

``post`` is the only tool whose availability varies: a daemon logged in with
``--read-only`` does not offer it. See :func:`tool_definitions`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from a4i import metadata, query
from a4i.errors import A4iError, NoDaemonError, NotLoggedInError, SessionExpiredError

# How much of a response this server will hand back. A class query against a
# real fabric can return tens of megabytes, which is not a failure of the query
# -- it is what was asked for -- but it would fill a context window and leave the
# model no room to do anything with it. Overridable by the person running the
# server, never by the model: a limit the caller can raise is one that will be
# raised the moment it binds.
MAX_BYTES_VAR = "A4I_MCP_MAX_BYTES"
DEFAULT_MAX_BYTES = 64 * 1024

# What to say when there is no session. The model cannot fix this itself -- the
# password is typed by a person into a terminal -- so the message says who has to
# act rather than what went wrong.
NO_SESSION = (
    "No APIC session. Ask the user to run 'a4i login <apic-host> -u <username>' "
    "in a terminal, then try again. There is no login tool here: the password is "
    "never handled by this server."
)


def max_bytes() -> int:
    """Return the response size limit, from the environment or the default."""

    raw = os.environ.get(MAX_BYTES_VAR)
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


# -- schemas ---------------------------------------------------------------

_KIND = {
    "type": "string",
    "enum": list(query.KINDS),
    "description": (
        "What 'target' names: 'class' for an ACI class name (fvTenant), "
        "'mo' for a distinguished name (uni/tn-common)."
    ),
}

_TARGET = {
    "type": "string",
    "description": "The class name or the DN, according to 'kind'. A DN needs no leading slash.",
}

_BODY = {
    "type": ["object", "array", "string"],
    "description": (
        'An ACI body: {"fvBD": {"attributes": {...}, "children": [...]}}, a list of '
        "those, or the same as JSON text. See the a4i://guide/post-body resource."
    ),
}

# The query parameters, shared by get. Each description is what the parameter
# does in ACI, not what a4i does with it, because ACI is what the model knows.
_QUERY_PROPERTIES: dict[str, Any] = {
    "query_target": {
        "type": "string",
        "enum": query.values(query.QueryTarget),
        "description": "Scope of the query: self (default), children, or subtree.",
    },
    "target_subtree_class": {
        "type": "string",
        "description": "Comma-separated class names limiting the queried scope.",
    },
    "query_target_filter": {
        "type": "string",
        "description": 'Filter on the queried scope, e.g. eq(fvTenant.name,"common").',
    },
    "rsp_subtree": {
        "type": "string",
        "enum": query.values(query.RspSubtree),
        "description": "How much of each MO's subtree to return: no (default), children, full.",
    },
    "rsp_subtree_class": {
        "type": "string",
        "description": "Comma-separated class names limiting the returned subtree.",
    },
    "rsp_subtree_filter": {
        "type": "string",
        "description": "Filter on the returned subtree.",
    },
    "rsp_subtree_include": {
        "type": "string",
        "description": (
            f"Comma-separated extra categories: {', '.join(query.values(query.RspSubtreeInclude))}."
        ),
    },
    "rsp_prop_include": {
        "type": "string",
        "enum": query.values(query.RspPropInclude),
        "description": (
            "Which properties to return. 'config-only' drops read-only properties and "
            "is much the smallest useful response; 'naming-only' returns just the DNs."
        ),
    },
    "order_by": {
        "type": "string",
        "description": "Sort the response, e.g. eventRecord.created|desc.",
    },
    "page": {"type": "integer", "minimum": 0, "description": "Page to return, counting from 0."},
    "page_size": {"type": "integer", "minimum": 1, "description": "Objects per page."},
    "node": {
        "type": "string",
        "description": (
            "Query a fabric switch directly by its management IP or hostname (not a node ID), "
            "using the same token. The switch's root is 'sys', not 'uni'."
        ),
    },
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


GET = _tool(
    "get",
    "GET objects from the ACI fabric, by class or by DN. Read-only. "
    "Responses over the size limit are refused with the total count and how to narrow "
    "them -- nothing is silently truncated or paged.",
    {"kind": _KIND, "target": _TARGET, **_QUERY_PROPERTIES},
    ["kind", "target"],
)

POST = _tool(
    "post",
    "POST a JSON body to the ACI fabric, creating, modifying or deleting objects. "
    "Run dry_run with the same arguments first: it sends nothing and shows what would "
    "change. There is no 'node' argument -- configuration must go through the APIC.",
    {"kind": _KIND, "target": _TARGET, "body": _BODY},
    ["kind", "target", "body"],
)

DRY_RUN = _tool(
    "dry_run",
    "Show what a POST of this body would change, without sending anything. Fetches the "
    "current state and reports the difference. An empty report means the body would "
    "change nothing at all. Works even when the session is read-only.",
    {"kind": _KIND, "target": _TARGET, "body": _BODY},
    ["kind", "target", "body"],
)

MERGE = _tool(
    "merge",
    "Fold several ACI bodies into the one body they describe between them, merged in "
    "order with later values winning attribute by attribute. Two bodies mean the same "
    "MO when they resolve to the same DN. The result is a polUni holding every merged "
    "MO nested under the MO it hangs off, which is what diff compares against and what "
    "a post to 'uni' takes. Every DN on the way down from 'uni' has to be described by "
    "something, unless 'loose' says to fill it in. Touches the fabric not at all. Use "
    "this whenever a configuration is spread across files: diff and post each take one "
    "body.",
    {
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Files to read the configuration from, or directories searched for "
                "*.json recursively. Merged in the order given."
            ),
        },
        "configs": {
            "type": "array",
            "items": {"type": ["object", "array"]},
            "description": (
                "Further ACI bodies given inline, merged after everything read from "
                "'paths' -- so these win. Use this for a body you have just written."
            ),
        },
        "output": {
            "type": "string",
            "description": (
                "Write the merged body to this file and return a summary instead of the "
                "body itself, then pass the same path to diff as 'path'. Do this for a "
                "configuration of any size: it keeps the whole body out of the "
                "conversation. An existing file is refused unless 'overwrite' is true."
            ),
        },
        "overwrite": {
            "type": "boolean",
            "description": "Allow 'output' to replace a file that already exists.",
        },
        "loose": {
            "type": "boolean",
            "description": (
                "Fill in an ancestor DN nothing describes -- the fvTenant of a BD written "
                "on its own -- where the bundled dictionary settles what class sits there. "
                "Off by default, and a filled-in ancestor is an MO the post may create that "
                "no input asked for."
            ),
        },
    },
    [],
)

DIFF = _tool(
    "diff",
    "Compare an intended configuration against everything under 'uni' on the fabric, "
    "both ways: what the configuration asks for and the fabric lacks, and what the "
    "fabric carries and the configuration never mentions. Reads only. Takes one body, "
    "as post does -- run merge first if the configuration is spread across files. Note "
    "that the configuration is taken to describe the whole of 'uni', so anything it "
    "omits is reported as extra -- use 'exclude' for the subtrees you are not describing.",
    {
        "body": {
            "type": ["object", "array", "string"],
            "description": (
                "The intended configuration as one ACI body: a polUni with children, a "
                "single MO, a list of them, or the same as JSON text. Give this or 'path'."
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "Read the one body from this file instead -- typically what merge wrote. "
                "A single file, not a directory: merge is what reads several."
            ),
        },
        "exclude": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "DNs to leave out along with everything under each, e.g. uni/tn-common. "
                "Neither side of the comparison reports them. A '*' makes one a pattern "
                "matching within a single RN -- uni/tn-test* is every tenant named "
                "test..., and uni/tn-*/BD-* every BD of every tenant. Everything else, "
                "brackets included, matches itself; '**' is not supported."
            ),
        },
        "expand": {
            "type": "boolean",
            "description": (
                "List every MO of a wholly missing or extra subtree instead of "
                "summarising it as its top MO with a count."
            ),
        },
    },
    [],
)

LIST = _tool(
    "list",
    "List ACI class names by prefix, or the DNs directly under a DN. "
    "With kind='class' this reads the bundled dictionary and needs no session; "
    "with kind='mo' it asks the fabric for one level of children.",
    {
        "kind": _KIND,
        "prefix": {
            "type": "string",
            "description": (
                "kind='class' only: return names starting with this, matched "
                "case-insensitively. Omit for every class name."
            ),
        },
        "dn": {
            "type": "string",
            "description": (
                "kind='mo' only: the parent DN whose children to list. "
                "Defaults to 'uni' (a switch's root is 'sys')."
            ),
        },
        "node": _QUERY_PROPERTIES["node"],
    },
    ["kind"],
)

DESCRIBE = _tool(
    "describe",
    "Describe one ACI class from the bundled model: what it is for, the properties you "
    "may set with their types, permitted values and defaults, how its RN is built, which "
    "classes contain it and which may hang under it. Call this before writing a body for "
    "a class. Needs no session.",
    {
        "class_name": {
            "type": "string",
            "description": "Exact ACI class name, case-sensitive, e.g. fvBD.",
        }
    },
    ["class_name"],
)

SEARCH = _tool(
    "search",
    "Find ACI classes by what they are called. Matches a substring of the class name, "
    "its label or its one-line summary, so 'bridge domain' finds fvBD. Use this when you "
    "do not know the class name; use list with kind='class' when you know how it begins. "
    "Needs no session.",
    {
        "keyword": {"type": "string", "description": "Words to look for, e.g. 'bridge domain'."},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Most results to return (default 40).",
        },
    },
    ["keyword"],
)

# The order tools are offered in is the order they are meant to be reached for.
# merge sits next to diff because it is the step before it, and it is offered to
# a read-only session too: it writes a local file at most, never the fabric.
ALL_TOOLS = [SEARCH, DESCRIBE, LIST, GET, DRY_RUN, POST, MERGE, DIFF]

WRITE_TOOLS = frozenset({"post"})


def tool_definitions(*, read_only: bool) -> list[dict]:
    """Return the tools to offer, leaving out what a read-only session cannot do.

    A tool that is offered and then refuses costs a call to find out. Leaving it
    out says the same thing before anything is spent -- and the instructions say
    what its absence means, so it does not read as an oversight.
    """

    if not read_only:
        return list(ALL_TOOLS)
    return [tool for tool in ALL_TOOLS if tool["name"] not in WRITE_TOOLS]


# -- handlers --------------------------------------------------------------


def _client():
    """Build a client that talks through the daemon, never starting one."""

    from a4i.client import Client
    from a4i.transport import DaemonTransport

    return Client(transport=DaemonTransport(autostart=False))


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _too_large(data: Any, text: str) -> str:
    """Return the refusal for a response too big to hand over.

    It says how much there was and what would make it smaller, because a bare
    "too large" leaves the only next move a blind retry.
    """

    total = data.get("totalCount") if isinstance(data, dict) else None
    counted = f"{total} objects" if total is not None else "the response"
    return (
        f"Response too large: {counted}, {len(text):,} bytes, over the {max_bytes():,} byte "
        f"limit. Nothing was truncated -- narrow the query and ask again:\n"
        "  - rsp_prop_include='config-only' drops every read-only property\n"
        "  - rsp_prop_include='naming-only' returns just the DNs\n"
        "  - target_subtree_class / rsp_subtree_class limit it to the classes you want\n"
        "  - page and page_size (given together, page counting from 0) take it a page at a time\n"
        f"  - a lower rsp_subtree, or a narrower target than {counted}"
    )


def _get(arguments: dict[str, Any]) -> str:
    data = _client().get(
        arguments["target"],
        kind=arguments["kind"],
        **{name: arguments.get(name) for name in _QUERY_PROPERTIES},
    )
    text = _json(data)
    if len(text.encode()) > max_bytes():
        raise ToolError(_too_large(data, text))
    return text


def _post(arguments: dict[str, Any]) -> str:
    data = _client().post(arguments["target"], arguments["body"], kind=arguments["kind"])
    return _json(data)


def _dry_run(arguments: dict[str, Any]) -> str:
    from a4i.output import dry_run_report

    changes = _client().dry_run(arguments["target"], arguments["body"], kind=arguments["kind"])
    return dry_run_report(changes)


def _merge(arguments: dict[str, Any]) -> str:
    from a4i import config
    from a4i.merge import UndescribedError, count, merge
    from a4i.validate import problems, refuse

    paths = list(arguments.get("paths") or [])
    try:
        configs: list[Any] = config.load(paths) if paths else []
    except OSError as exc:
        raise ToolError(f"cannot read the configuration: {exc}") from None
    # Inline bodies go last, so a body written here wins over what the files
    # say, which is the point of writing one. They are checked here rather than
    # left to merge, so that one is named by its position in this argument
    # rather than by its position after the files were counted in.
    inline = list(arguments.get("configs") or [])
    refuse([p for i, body in enumerate(inline) for p in problems(body, f"configs[{i}]")])
    configs.extend(inline)
    if not configs:
        raise ToolError("give 'paths' (files or directories) or 'configs' (inline ACI bodies)")
    try:
        body = merge(*configs, loose=bool(arguments.get("loose")))
    except UndescribedError as exc:
        # The rule is merge's; the way out is this tool's own argument, so it is
        # named here -- as 'overwrite' is below.
        raise ToolError(
            f"{exc} (pass loose: true to fill {'it' if exc.count == 1 else 'them'} in)"
        ) from None
    text = _json(body)

    output = arguments.get("output")
    if output is None:
        if len(text.encode()) > max_bytes():
            raise ToolError(
                f"The merged configuration is {len(text):,} bytes, over the "
                f"{max_bytes():,} byte limit. Nothing was truncated -- pass 'output' with a "
                "file path to write it there instead, then give that path to diff as 'path'."
            )
        return text
    try:
        config.write(output, text, overwrite=bool(arguments.get("overwrite")))
    except FileExistsError as exc:
        # Before the OSError below, which it is one of. The rule is the config
        # module's; the way out is this tool's own argument, so it is named here.
        raise ToolError(f"{exc} (pass overwrite: true to replace it)") from None
    except OSError as exc:
        raise ToolError(f"cannot write {output}: {exc}") from None
    return f"merged {count(body)} MOs into {output}; pass it to diff as path='{output}'"


def _diff(arguments: dict[str, Any]) -> str:
    from pathlib import Path

    from a4i.output import diff_report

    body = arguments.get("body")
    path = arguments.get("path")
    if (body is None) == (path is None):
        raise ToolError("give exactly one of 'body' (an ACI body) or 'path' (a file holding one)")
    if path is not None:
        target = Path(path)
        if target.is_dir():
            raise ToolError(
                f"{path} is a directory, and diff compares one configuration. Run merge over "
                "it with an 'output' path, then give that file here."
            )
        try:
            body = target.read_text()
        except OSError as exc:
            raise ToolError(f"cannot read the configuration: {exc}") from None
    changes = _client().diff(
        body,
        expand=bool(arguments.get("expand")),
        exclude=list(arguments.get("exclude") or []) or None,
    )
    return diff_report(changes)


def _list(arguments: dict[str, Any]) -> str:
    kind = arguments["kind"]
    if kind == "class":
        names = metadata.class_names_startingwith(arguments.get("prefix") or "")
        if not names:
            return "no class names match that prefix"
        return "\n".join(names)
    if kind != "mo":
        raise ToolError(f"invalid kind: {kind!r} (choose from {', '.join(query.KINDS)})")

    dns = _client().list_children(arguments.get("dn") or "uni", node=arguments.get("node"))
    return "\n".join(dns) if dns else "no child MOs"


def _describe(arguments: dict[str, Any]) -> str:
    class_name = arguments["class_name"]
    record = metadata.describe(class_name)
    if record is None:
        hint = ""
        near = metadata.search(class_name, limit=5)
        if near:
            hint = "\nDid you mean: " + ", ".join(name for name, _, _ in near)
        raise ToolError(
            f"the bundled model does not carry {class_name!r}. Class names are "
            f"case-sensitive, and the fabric may be newer than the dictionary; "
            f"you can still query the class either way.{hint}"
        )
    return _json(record)


def _search(arguments: dict[str, Any]) -> str:
    limit = arguments.get("limit") or 40
    results = metadata.search(arguments["keyword"], limit=limit)
    if not results:
        return f"no classes match {arguments['keyword']!r}"
    return "\n".join(f"{name}\t{label}\t{summary}" for name, label, summary in results)


_HANDLERS = {
    "get": _get,
    "post": _post,
    "dry_run": _dry_run,
    "merge": _merge,
    "diff": _diff,
    "list": _list,
    "describe": _describe,
    "search": _search,
}


class ToolError(Exception):
    """A tool call that failed for a reason the model can act on."""


def call(name: str, arguments: dict[str, Any]) -> str:
    """Run one tool and return its text result.

    Raises :class:`ToolError` with what to do about it. Every failure a caller
    can do something about arrives that way, including the APIC's own: a model
    reads "not logged in" and asks the user to log in, where a stack trace would
    tell it nothing.
    """

    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool: {name}")
    try:
        return handler(arguments)
    except ToolError:
        raise
    except (NotLoggedInError, SessionExpiredError, NoDaemonError):
        # No daemon is the ordinary state before anyone logs in, and it is the
        # same problem as no session as far as the model is concerned. A socket
        # this client refuses to use is not: that one is a misconfiguration on
        # the machine, and saying "log in" about it would send the user in
        # circles, so it falls through to the message it came with.
        raise ToolError(NO_SESSION) from None
    except A4iError as exc:
        # Everything else a4i raises, in the words it was raised with: the
        # APIC's own complaint, a read-only session, a socket left unusable.
        raise ToolError(str(exc)) from None
    except OSError as exc:
        # A dictionary that is not there, or a configuration path that cannot be
        # read. Neither is a protocol failure, and a model told plainly can pick
        # a different tool rather than retrying the same one.
        raise ToolError(str(exc)) from None
    except (ValueError, TypeError, KeyError) as exc:
        # A malformed argument, an unparseable body, or a combination ACI does
        # not define -- all of them the model's to fix.
        raise ToolError(str(exc)) from None
