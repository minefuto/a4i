"""How a request reaches the APIC.

:class:`~a4i.client.Client` holds everything a get or a post means -- the
parameters, the validation, the dry-run comparison -- and knows nothing about
how the request travels. That is the only difference between the two entry
points: the CLI sends it over a Unix domain socket to the daemon holding the
token, while a library caller sends it from a :class:`~a4i.session.Session` of
its own.

A daemon reply flattens the failure into a :class:`~a4i.ipc.DaemonError` carrying
a type, so :class:`DaemonTransport` restores the typed exception. A caller then
catches the same exception whichever transport is underneath.

The awaited transports are the same layer over an
:class:`~a4i.session.AsyncSession`. There is no awaited daemon transport: the
daemon exists to carry a token across the short-lived processes a CLI run is
made of, which is not a problem an ``async with`` block has.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from a4i import query
from a4i.errors import ApicError, NotLoggedInError, ReadOnlyError, SessionExpiredError
from a4i.ipc import DaemonError, request

if TYPE_CHECKING:
    from a4i.session import AsyncSession, Session


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

    ``autostart`` is what a command wants and a server does not. A command is
    something a person just typed, so starting a daemon to serve it is the
    obvious thing; the MCP server is started by whatever launched the editor,
    and a daemon spawned on that account would be one nobody asked for and
    nobody is logged in to.
    """

    def __init__(self, *, autostart: bool = True) -> None:
        self._autostart = autostart

    def get(self, target: str, kind: str, params: dict[str, str] | None, node: str | None) -> Any:
        return self._request("get", target=target, kind=kind, params=params or {}, node=node)

    def post(self, target: str, kind: str, body: str) -> Any:
        return self._request("post", target=target, kind=kind, body=body)

    def _request(self, op: str, **args: Any) -> Any:
        try:
            return request(op, autostart=self._autostart, **args)
        except DaemonError as exc:
            raise _restore(exc) from None


def _restore(exc: DaemonError) -> Exception:
    """Turn a daemon error reply back into the exception the daemon caught.

    A type this client does not recognise, and "socket" -- which no daemon ever
    raised, because it means we refused to talk to one -- stay as they are.
    """

    if exc.type == "apic":
        return ApicError(str(exc), code=exc.code)
    if exc.type == "expired":
        return SessionExpiredError(str(exc))
    if exc.type == "not_logged_in":
        return NotLoggedInError(str(exc))
    if exc.type == "read_only":
        return ReadOnlyError(str(exc))
    return exc
