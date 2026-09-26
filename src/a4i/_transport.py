# There is no awaited daemon transport: the daemon exists to carry a token across the
# short-lived processes a CLI run is made of, which is not a problem an async with block
# has.

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from a4i import _ipc as ipc
from a4i import _query as query

if TYPE_CHECKING:
    from a4i._session import AsyncSession, Session


class Transport(Protocol):
    def get(
        self, target: str, kind: str, params: dict[str, str] | None, node: str | None
    ) -> Any: ...

    def post(self, target: str, kind: str, body: str) -> Any: ...


class DirectTransport:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, target: str, kind: str, params: dict[str, str] | None, node: str | None) -> Any:
        return self.session.get(query.build_path(target, kind), params or None, host=node)

    def post(self, target: str, kind: str, body: str) -> Any:
        return self.session.post(query.build_path(target, kind), body)


class AsyncTransport(Protocol):
    async def get(
        self, target: str, kind: str, params: dict[str, str] | None, node: str | None
    ) -> Any: ...

    async def post(self, target: str, kind: str, body: str) -> Any: ...


class AsyncDirectTransport:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, target: str, kind: str, params: dict[str, str] | None, node: str | None
    ) -> Any:
        return await self.session.get(query.build_path(target, kind), params or None, host=node)

    async def post(self, target: str, kind: str, body: str) -> Any:
        return await self.session.post(query.build_path(target, kind), body)


# autostart is what a command wants and a server does not: the MCP server is started by
# whatever launched the editor, and a daemon spawned on that account is one nobody asked
# for and nobody is logged in to.
class DaemonTransport:
    def __init__(self, *, autostart: bool = True) -> None:
        self._autostart = autostart

    def get(self, target: str, kind: str, params: dict[str, str] | None, node: str | None) -> Any:
        return ipc.get(target, kind, params, node, autostart=self._autostart)

    def post(self, target: str, kind: str, body: str) -> Any:
        return ipc.post(target, kind, body, autostart=self._autostart)
