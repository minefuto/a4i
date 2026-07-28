"""a4i as an MCP server, so that a model can read and write an ACI fabric.

``a4i mcp`` speaks MCP over stdio and forwards every request to the daemon
already holding the APIC token, exactly as a command does. That is the whole of
the arrangement: the daemon knows nothing about MCP, no port is opened, and the
token stays where it was -- in one process's memory, behind a socket in a
directory only its owner can use.

The server starts whether or not anyone is logged in, because an MCP client
launches it when the editor starts and the person logs in afterwards. Until then
every tool that needs the fabric says so and names the command to run.

:mod:`a4i.mcp.server` is the protocol, :mod:`a4i.mcp.tools` the seven tools, and
:mod:`a4i.mcp.guides` the four documents the model is offered as resources.
"""

from __future__ import annotations

from a4i.mcp.server import serve

__all__ = ["serve"]
