# a4i mcp forwards every request to the daemon already holding the APIC token, exactly
# as a command does: the daemon knows nothing about MCP, no port is opened, and the
# token stays where it was. The server starts whether or not anyone is logged in,
# because an MCP client launches it when the editor starts and the person logs in
# afterwards.

from __future__ import annotations

from a4i._mcp.server import serve

__all__ = ["serve"]
