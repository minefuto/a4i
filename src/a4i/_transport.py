"""How a request reaches the APIC.

The one difference between the two entry points: the CLI sends over a Unix
domain socket to the daemon holding the token, a library caller from a
:class:`~a4i._session.Session` of its own. A caller catches the same exception
either way, :mod:`a4i._ipc` rebuilding what the daemon flattened onto the wire.

There is no awaited daemon transport: the daemon exists to carry a token across
the short-lived processes a CLI run is made of, which is not a problem an
``async with`` block has.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from a4i import _ipc as ipc
from a4i import _query as query

if TYPE_CHECKING:
    from a4i._session import AsyncSession, Session


class Transport(Protocol):
    """Sends one request and returns the APIC's parsed response.

    ``kind`` says what ``target`` names -- ``"class"`` or ``"mo"`` -- and travels
    with it all the way down, so the path is built from what the caller meant
    rather than from the shape of the string.
    """

    def get(
        self, target: str, kind: str, params: dict[str, str] | None, node: str | None
    ) -> Any: ...

    def post(self, target: str, kind: str, body: str) -> Any: ...


class DirectTransport:
    """Talks to the APIC from this process, over a session we own."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, target: str, kind: str, params: dict[str, str] | None, node: str | None) -> Any:
        return self.session.get(query.build_path(target, kind), params or None, host=node)

    def post(self, target: str, kind: str, body: str) -> Any:
        return self.session.post(query.build_path(target, kind), body)


class AsyncTransport(Protocol):
    """:class:`Transport`, awaited. The arguments and the result are the same."""

    async def get(
        self, target: str, kind: str, params: dict[str, str] | None, node: str | None
    ) -> Any: ...

    async def post(self, target: str, kind: str, body: str) -> Any: ...


class AsyncDirectTransport:
    """Talks to the APIC from this process, over an awaited session we own."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, target: str, kind: str, params: dict[str, str] | None, node: str | None
    ) -> Any:
        return await self.session.get(query.build_path(target, kind), params or None, host=node)

    async def post(self, target: str, kind: str, body: str) -> Any:
        return await self.session.post(query.build_path(target, kind), body)


class DaemonTransport:
    """Hands the request to the daemon that holds the token.

    ``autostart`` is what a command wants and a server does not: the MCP server
    is started by whatever launched the editor, and a daemon spawned on that
    account is one nobody asked for and nobody is logged in to.
    """

    def __init__(self, *, autostart: bool = True) -> None:
        self._autostart = autostart

    def get(self, target: str, kind: str, params: dict[str, str] | None, node: str | None) -> Any:
        return ipc.get(target, kind, params, node, autostart=self._autostart)

    def post(self, target: str, kind: str, body: str) -> Any:
        return ipc.post(target, kind, body, autostart=self._autostart)
