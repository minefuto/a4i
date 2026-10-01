# Every tool is a thin wrapper over the same a4i._client.Client the command line drives,
# so a model and a person send the identical request. What is added is the part a model
# needs and a person does not: a schema, a size limit so that one query cannot fill a
# context window, and errors phrased as instructions.

from __future__ import annotations

import json
import os
from typing import Any

from a4i import _metadata as metadata
from a4i import _query as query
from a4i._errors import (
    A4iError,
    NoDaemonError,
    NoFabricError,
    NotLoggedInError,
    SessionExpiredError,
)

# How much of a response this server will hand back: a class query against a real
# fabric can return tens of megabytes and leave the model no room to act on it.
# Overridable by the person running the server, never by the model, a limit the
# caller can raise being one that will be raised the moment it binds.
MAX_BYTES_VAR = "A4I_MCP_MAX_BYTES"
DEFAULT_MAX_BYTES = 64 * 1024

# What to say when there is no session. The model cannot fix this itself, so the
# message says who has to act rather than what went wrong.
NO_SESSION = (
    "No APIC session. Ask the user to run 'a4i login <apic-host> -u <username>' "
    "in a terminal, then try again. There is no login tool here: the password is "
    "never handled by this server."
)


def max_bytes() -> int:
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
        "those, or the same as JSON text. Give this or 'path'. See the "
        "a4i://guide/post-body resource."
    ),
}

_PATH = {
    "type": "string",
    "description": (
        "A file holding the body, read and used exactly as 'body' would be -- one file, "
        "no pattern, nothing folded. Give this or 'body'. Use it for a body merge or "
        "plan wrote out, so the body never travels through this conversation."
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


# The intended configuration, as merge, diff and plan each take it: exactly one of these.
_CONFIG: dict[str, Any] = {
    "body": {
        "type": ["object", "array", "string"],
        "description": (
            "The configuration as one ACI body: a polUni with children, a single MO, a "
            "list of them (folded in order, later values winning), or the same as JSON "
            "text. Give this or 'paths'."
        ),
    },
    "paths": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Files to read the configuration from instead, each a path or a glob pattern "
            "('conf/**/*.json' reaches into subdirectories), folded in order with later "
            "values winning. A pattern's matches are taken in path order. A directory is "
            "refused: give a pattern for the files in it."
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
    "change. For a whole configuration, run plan instead and post the body it returns, "
    "which holds only the MOs that change. There is no 'node' argument -- configuration "
    "must go through the APIC.",
    {"kind": _KIND, "target": _TARGET, "body": _BODY, "path": _PATH},
    ["kind", "target"],
)

DRY_RUN = _tool(
    "dry_run",
    "Show what a POST of this body would change, without sending anything. An empty "
    "report means the body would change nothing at all. If fetch has read the fabric "
    "into the session, that is what this compares against and nothing goes out; if it "
    "has not, this reads the subtrees the body itself names, which for a single MO is "
    "far less than a whole fetch. The report says which of the two it was. So there is "
    "no need to fetch first for this -- fetch is for diff and plan. Works even when the "
    "session is read-only.",
    {"kind": _KIND, "target": _TARGET, "body": _BODY, "path": _PATH},
    ["kind", "target"],
)

MERGE = _tool(
    "merge",
    "Fold several ACI bodies into the one body they describe between them, merged in "
    "order with later values winning attribute by attribute. Two bodies mean the same "
    "MO when they resolve to the same DN. The result is a polUni holding every merged "
    "MO nested under the MO it hangs off, which is what diff compares against and what "
    "a post to 'uni' takes. Every DN on the way down from 'uni' has to be described by "
    "something, unless 'loose' says to fill it in. Touches the fabric not at all. Use "
    "this to write a configuration spread across files out as the one body post takes.",
    {
        **_CONFIG,
        "output": {
            "type": "string",
            "description": (
                "Write the merged body to this file and return a summary instead of the "
                "body itself, then pass the same path to dry_run as 'path', and to diff "
                "and plan as 'paths'. Do this for a "
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
    "fabric carries and the configuration never mentions. Reads only, and sends nothing "
    "to the APIC: the fabric side is what fetch last read, so call fetch first, and "
    "again after any post. Several files are folded as merge folds them. Note "
    "that the configuration is taken to describe the whole of 'uni', so anything it "
    "omits is reported as extra -- use 'exclude' for the subtrees you are not describing.",
    {
        **_CONFIG,
        "exclude": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "DNs to leave out along with everything under each, e.g. uni/tn-common. "
                "Neither side of the comparison reports them. A '*' makes one a pattern "
                "matching within a single RN -- uni/tn-test* is every tenant named "
                "test..., and uni/tn-*/BD-* every BD of every tenant. Everything else, "
                "brackets included, matches itself; '**' is not supported. A leading '!' "
                "makes one an exception to the others, so ['uni/tn-*', '!uni/tn-mgmt'] "
                "leaves out every tenant but that one; the deepest name given wins, and "
                "exceptions on their own are refused. A trailing [key=value] narrows one "
                "to the MOs whose attribute matches -- uni/tn-*/BD-*[descr=auto-*] -- for "
                "what a DN cannot tell apart; the value is a '*' pattern, one condition "
                "per name, and either side's value is enough to leave the MO out."
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

FETCH = _tool(
    "fetch",
    "Read the whole fabric into the session and hold it there, for diff and plan to "
    "compare against. Reads only, and works when the session is read-only. Returns how "
    "much was read, not the configuration itself: a whole fabric runs to megabytes, and "
    "nothing here needs to see it -- diff and plan take their fabric side from what this "
    "left behind. Call it before either of them, and again after a post: posting drops "
    "what was read, because it is no longer what the fabric holds. A login, a logout and "
    "a session expiry drop it too. diff and plan say so and stop when nothing is held.",
    {},
    [],
)

PLAN = _tool(
    "plan",
    "Narrow an intended configuration to the MOs posting it would change, and return "
    "that as a body to post at 'uni'. Reads only, and sends nothing to the APIC: the "
    "fabric side is what fetch last read, so call fetch first, and again after any "
    "post. Use this between merge and post: posting the whole configuration hands "
    "the APIC every MO it already agrees with, and the APIC writes all of them. Run "
    "dry_run on the same body first -- that is the report on what changes, from the "
    "same fetched fabric, and this returns no report of its own. The result is a "
    "polUni shaped as merge shapes one, holding the changed MOs and the MOs they hang "
    'under -- those carry rn and status="modified" only, so the post fails rather than '
    "creating one the fabric turns out not to have. The only MOs it can create are the "
    'ones it marks status="created". Refused if the comparison warns the post '
    "would fail.",
    {
        **_CONFIG,
        "output": {
            "type": "string",
            "description": (
                "Write the body to this file and return a summary instead of the body "
                "itself, then give the same path to post as 'path'. Do this for a "
                "configuration of any size: it keeps the whole body out of the "
                "conversation. An existing file is refused unless 'overwrite' is true."
            ),
        },
        "overwrite": {
            "type": "boolean",
            "description": "Allow 'output' to replace a file that already exists.",
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
# merge is offered to a read-only session too: it writes a local file at most.
ALL_TOOLS = [SEARCH, DESCRIBE, LIST, GET, DRY_RUN, POST, MERGE, FETCH, PLAN, DIFF]

WRITE_TOOLS = frozenset({"post"})


# A tool that is offered and then refuses costs a call to find out. Leaving it out says
# the same thing before anything is spent -- and the instructions say what its absence
# means, so it does not read as an oversight.
def tool_definitions(*, read_only: bool) -> list[dict]:
    if not read_only:
        return list(ALL_TOOLS)
    return [tool for tool in ALL_TOOLS if tool["name"] not in WRITE_TOOLS]


# -- handlers --------------------------------------------------------------


def _client():
    from a4i._client import Client
    from a4i._transport import DaemonTransport

    return Client(_transport=DaemonTransport(autostart=False))


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _too_large(data: Any, text: str) -> str:
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
    data = _client().post(arguments["target"], _body_or_path(arguments), kind=arguments["kind"])
    return _json(data)


# The file's text is handed on untouched, so a post sends it exactly as written, as
# 'a4i post mo uni plan.json' does.
def _body_or_path(arguments: dict[str, Any]) -> Any:
    from pathlib import Path

    body, path = arguments.get("body"), arguments.get("path")
    if (body is None) == (path is None):
        raise ToolError("give exactly one of 'body' (an ACI body) or 'path' (a file holding one)")
    if path is None:
        return body
    try:
        return Path(path).read_text()
    except OSError as exc:
        raise ToolError(f"cannot read {path}: {exc}") from None


# The report says which fabric it compared against, for the reason post --dry-run prints
# it.
def _dry_run(arguments: dict[str, Any]) -> str:
    from a4i import _dry_run as dry_run
    from a4i import _ipc as ipc

    target, body, kind = arguments["target"], _body_or_path(arguments), arguments["kind"]
    try:
        fabric = ipc.fabric()
    except NoFabricError:
        changes = _client().dry_run(target, body, kind=kind)
        note = "Read the subtrees this body names; nothing had been fetched."
    else:
        changes = dry_run.check(target, body, kind=kind, fabric=fabric)
        note = "Compared against the fabric fetch read."
    return f"{changes}\n\n{note}"


def _merge(arguments: dict[str, Any]) -> str:
    from a4i import _config as config
    from a4i._merge import UndescribedError, count, merge

    try:
        body = merge(_one_body(arguments), loose=bool(arguments.get("loose")))
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
                "file path to write it there instead, then give that path to diff as 'paths'."
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
    return (
        f"merged {count(body)} MOs into {output}; pass it to dry_run as path='{output}', "
        f"and to diff and plan as paths=['{output}']"
    )


def _fetch(arguments: dict[str, Any]) -> str:
    from a4i import _ipc as ipc

    # autostart=False, as every other request from this server: a daemon started
    # here would be one nobody is logged in to.
    held = ipc.fetch(autostart=False)
    return (
        f"read {held['count']:,} MOs into the session cache; "
        "diff, plan and dry runs will use it until the next post"
    )


def _one_body(arguments: dict[str, Any]) -> Any:
    import glob

    from a4i import _config as config
    from a4i._validate import read_body

    body = arguments.get("body")
    patterns = arguments.get("paths") or []
    if (body is None) == (not patterns):
        raise ToolError(
            "give exactly one of 'body' (an ACI body) or 'paths' (files or glob patterns)"
        )
    if body is not None:
        return read_body(body)[1]
    # There is no shell here to expand a pattern, so this does it, as a shell would.
    # And with no path at all, load would read stdin, which is this server's protocol.
    paths: list[str] = []
    for pattern in patterns:
        found = sorted(glob.glob(pattern, recursive=True))
        if not found:
            raise ToolError(f"{pattern} matches no file")
        paths.extend(found)
    try:
        return config.load(paths)
    except OSError as exc:
        raise ToolError(f"cannot read the configuration: {exc}") from None


def _plan(arguments: dict[str, Any]) -> str:
    from a4i import _config as config
    from a4i import _ipc as ipc
    from a4i import _plan as plan_
    from a4i._merge import count
    from a4i._output import plural

    body = _one_body(arguments)
    try:
        narrowed = plan_.create(body, fabric=ipc.fabric())
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    text = _json(narrowed)
    output = arguments.get("output")
    if output is None:
        if len(text.encode()) > max_bytes():
            raise ToolError(
                f"The plan is {len(text):,} bytes, over the {max_bytes():,} byte limit. "
                "Nothing was truncated -- pass 'output' with a file path to write it there "
                "instead, then give that path to post as 'path'."
            )
        return text
    try:
        config.write(output, text, overwrite=bool(arguments.get("overwrite")))
    except FileExistsError:
        raise ToolError(f"{output} exists. Pass overwrite: true to replace it.") from None
    except OSError as exc:
        raise ToolError(f"cannot write {output}: {exc}") from None
    return f"wrote {plural(count(narrowed), 'MO')} to {output}"


def _diff(arguments: dict[str, Any]) -> str:
    from a4i import _diff as diff
    from a4i import _ipc as ipc

    body = _one_body(arguments)
    changes = diff.compare(
        body,
        fabric=ipc.fabric(),
        expand=bool(arguments.get("expand")),
        exclude=list(arguments.get("exclude") or []) or None,
    )
    return str(changes)


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
    "fetch": _fetch,
    "plan": _plan,
    "diff": _diff,
    "list": _list,
    "describe": _describe,
    "search": _search,
}


class ToolError(Exception):
    pass


def call(name: str, arguments: dict[str, Any]) -> str:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool: {name}")
    try:
        return handler(arguments)
    except ToolError:
        raise
    except (NotLoggedInError, SessionExpiredError, NoDaemonError):
        # No daemon is the ordinary state before anyone logs in: the same problem
        # as no session, as far as the model is concerned. An unusable socket is
        # not, being a misconfiguration on the machine, so it falls through to the
        # message it came with.
        raise ToolError(NO_SESSION) from None
    except A4iError as exc:
        # Everything else a4i raises, in the words it was raised with: the
        # APIC's own complaint, a read-only session, a socket left unusable.
        raise ToolError(str(exc)) from None
    except OSError as exc:
        # A dictionary that is not there, or a configuration path that cannot be
        # read. Neither is a protocol failure, and a model told plainly can pick a
        # different tool.
        raise ToolError(str(exc)) from None
    except (ValueError, TypeError, KeyError) as exc:
        # A malformed argument, an unparseable body, or a combination ACI does
        # not define -- all of them the model's to fix.
        raise ToolError(str(exc)) from None
