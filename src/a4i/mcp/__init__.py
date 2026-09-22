"""a4i as an MCP server, so that a model can read and write an ACI fabric.

``a4i mcp`` speaks MCP over stdio and forwards every request to the daemon
already holding the APIC token, exactly as a command does: the daemon knows
nothing about MCP, no port is opened, and the token stays where it was.

The server starts whether or not anyone is logged in, because an MCP client
launches it when the editor starts and the person logs in afterwards.

:mod:`a4i.mcp.server` is the protocol, :mod:`a4i.mcp.tools` the ten tools, and
:mod:`a4i.mcp.guides` the four documents offered as resources.
"""

from __future__ import annotations

from a4i.mcp.server import serve

__all__ = ["serve"]
