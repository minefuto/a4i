"""The MCP server itself: JSON-RPC 2.0 over newline-delimited stdio.

Written out rather than taken from the MCP SDK, for the reason argparse is here
instead of Typer: this process is started afresh every time an MCP client
launches, and the SDK's import costs more than everything it would be doing. The
surface actually used is five methods and one notification, none of which needs
a framework.

The protocol is one JSON object per line, in both directions. A request carries
an ``id`` and gets exactly one reply; a notification carries none and gets none.

The one thing here that is not plain request-and-reply is the tool list. Whether
``post`` is offered depends on whether the daemon was logged in read-only, and
the daemon is usually not logged in at all when a client starts up and asks. So
the list is built from the daemon's state each time it is asked for, and when
that state turns out to have changed, a ``notifications/tools/list_changed`` goes
out and the client asks again.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from a4i.ipc import DaemonError, request
from a4i.mcp import guides, tools

# Protocol revisions this server can speak. A client asking for one of them is
# answered in its own; anything else is answered in the newest we know, which is
# what the specification asks a server to do when it cannot meet the request.
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_VERSION = SUPPORTED_VERSIONS[0]

SERVER_NAME = "a4i"

RESOURCE_PREFIX = "a4i://guide/"
RESOURCE_MIME = "text/markdown"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class Server:
    """One MCP session.

    :meth:`handle` takes one parsed message and returns the messages to send
    back -- usually one reply, sometimes a notification alongside it, and nothing
    at all for a notification from the client. Keeping the transport out of it is
    what lets the protocol be tested by handing it messages.
    """

    def __init__(self) -> None:
        self._protocol_version = LATEST_VERSION
        # What the client was last told the tool list depends on. None means it
        # has not been told anything yet, so there is nothing to contradict.
        self._announced_read_only: bool | None = None

    # -- daemon state -----------------------------------------------------

    def read_only(self) -> bool:
        """Return whether the daemon is holding a read-only session.

        A daemon that is not running, or not logged in, is not read-only as far
        as this is concerned. That matters: the client asks for the tool list
        before anyone has logged in, and hiding ``post`` on the strength of a
        session that does not exist yet would hide it from the ordinary case as
        well -- a client that lists tools once at startup would never see it
        again. Only a session known to be read-only takes it away.
        """

        try:
            status = request("status", autostart=False)
        except DaemonError:
            return False
        return bool(status.get("read_only")) if isinstance(status, dict) else False

    def _list_changed(self) -> list[dict[str, Any]]:
        """Return the notification to send if the tool list is no longer what we said."""

        current = self.read_only()
        if self._announced_read_only is None or self._announced_read_only == current:
            return []
        self._announced_read_only = current
        return [{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}]

    # -- dispatch ---------------------------------------------------------

    def handle(self, message: Any) -> list[dict[str, Any]]:
        """Return the messages to send in response to one incoming message."""

        if not isinstance(message, dict):
            return [_error(None, INVALID_REQUEST, "message must be a JSON object")]
        method = message.get("method")
        message_id = message.get("id")
        if not isinstance(method, str):
            return [_error(message_id, INVALID_REQUEST, "missing method")]
        # A notification has no id and takes no reply, whatever it asked for.
        if message_id is None:
            return []

        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        handler = getattr(self, f"_on_{method.replace('/', '_')}", None)
        if handler is None:
            return [_error(message_id, METHOD_NOT_FOUND, f"unknown method: {method}")]
        try:
            result = handler(params)
        except _RequestError as exc:
            return [_error(message_id, exc.code, str(exc))]
        except Exception as exc:  # noqa: BLE001 - reported to the client, never printed
            return [_error(message_id, INTERNAL_ERROR, str(exc))]
        return [{"jsonrpc": "2.0", "id": message_id, "result": result}, *self._list_changed()]

    # -- methods ----------------------------------------------------------

    def _on_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        import a4i

        asked = params.get("protocolVersion")
        self._protocol_version = asked if asked in SUPPORTED_VERSIONS else LATEST_VERSION
        return {
            "protocolVersion": self._protocol_version,
            "capabilities": {
                # listChanged, because whether post is offered follows the
                # daemon's session, which outlives no client and starts after
                # most of them.
                "tools": {"listChanged": True},
                "resources": {},
            },
            "serverInfo": {"name": SERVER_NAME, "version": a4i.__version__},
            "instructions": guides.INSTRUCTIONS,
        }

    def _on_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def _on_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        read_only = self.read_only()
        self._announced_read_only = read_only
        return {"tools": tools.tool_definitions(read_only=read_only)}

    def _on_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise _RequestError(INVALID_PARAMS, "missing tool name")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            text = tools.call(name, arguments)
        except tools.ToolError as exc:
            # A tool that failed is a result, not a protocol error: the model is
            # meant to read what went wrong and try something else, which it
            # cannot do with a JSON-RPC error.
            return _content(str(exc), is_error=True)
        return _content(text)

    def _on_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "resources": [
                {
                    "uri": f"{RESOURCE_PREFIX}{name}",
                    "name": name,
                    "title": title,
                    "description": description,
                    "mimeType": RESOURCE_MIME,
                }
                for name, (title, description, _) in guides.GUIDES.items()
            ]
        }

    def _on_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith(RESOURCE_PREFIX):
            raise _RequestError(INVALID_PARAMS, f"unknown resource: {uri!r}")
        guide = guides.GUIDES.get(uri[len(RESOURCE_PREFIX) :])
        if guide is None:
            raise _RequestError(INVALID_PARAMS, f"unknown resource: {uri!r}")
        return {"contents": [{"uri": uri, "mimeType": RESOURCE_MIME, "text": guide[2]}]}


class _RequestError(Exception):
    """A JSON-RPC level failure: the request itself could not be acted on."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _content(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Run the server over ``stdin``/``stdout`` until the client closes the stream.

    Nothing is ever printed to stdout except protocol messages -- a stray print
    would be read as one and end the session -- so anything worth saying goes to
    stderr, which the client shows as the server's log.
    """

    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    server = Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            _write(stdout, _error(None, PARSE_ERROR, str(exc)))
            continue
        for reply in server.handle(message):
            _write(stdout, reply)
    return 0


def _write(stdout: TextIO, message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    stdout.flush()
