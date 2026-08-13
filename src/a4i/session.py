"""APIC HTTP session.

Holds the authentication token in memory only (never persisted) and implements
the command-driven lazy refresh policy: the token is refreshed on demand once
half of its lifetime has elapsed, and is considered expired once the full
lifetime has elapsed without any activity.

:class:`Session` sends its requests and :class:`AsyncSession` awaits them. What
an authentication *is* -- which token is held, whether it is due for a refresh
or over, and what an APIC response means -- is the same either way and lives in
:class:`_SessionBase`, so the two cannot come to answer the same question
differently.
"""

from __future__ import annotations

import os
import ssl
import time
from collections import OrderedDict
from collections.abc import Callable
from functools import partial
from typing import Any, Generic, TypeVar

import httpx2

from a4i.errors import ApicError, NotLoggedInError, SessionExpiredError

# Re-exported: this module raises them, and importing them from here reads more
# naturally than from a4i.errors at the call sites that already use a Session.
__all__ = [
    "DEFAULT_REFRESH_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "LOGOUT_TIMEOUT",
    "NODE_CLIENT_MAX",
    "ApicError",
    "AsyncSession",
    "NotLoggedInError",
    "Session",
    "SessionExpiredError",
    "normalize_base_url",
]

DEFAULT_REFRESH_TIMEOUT = 600.0
_HTTP_ERROR_STATUS = 400

# How long a single request may take before it is given up on. Named apart from
# DEFAULT_REFRESH_TIMEOUT, which is the token's lifetime and not a limit on
# anything this side sends. A session takes its own value at construction, so an
# APIC that answers slowly is a setting rather than a wall.
DEFAULT_TIMEOUT = 30.0

# Logging out is the last thing a command or a daemon does, so an APIC that has
# stopped answering must not hold the exit up for the client's full timeout.
LOGOUT_TIMEOUT = 5.0

# Fabric switches accept the APIC token, so a session may end up talking to many
# of them. Each client owns a connection pool, so only the few most recently used
# hosts are kept alive.
NODE_CLIENT_MAX = 8

# The two httpx2 clients a session can be built on. What this module asks of one
# -- a cookie jar, a request, a close -- is spelled the same on either, but the
# two are typed apart, so the shared code below is written against whichever the
# subclass settled on.
ClientT = TypeVar("ClientT", httpx2.Client, httpx2.AsyncClient)


def normalize_base_url(host: str) -> str:
    """Normalize a user-supplied host into an ``https://host`` base URL."""

    host = host.strip()
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    return host.rstrip("/")


def _ssl_context(verify: bool | str) -> ssl.SSLContext | bool:
    """Turn a CA bundle path into an SSL context, passing a bool through.

    ``verify`` is what ``--ca`` and the ``verify`` argument accept: True for the
    system trust store, False to skip verification, or the path to a CA of one's
    own. httpx2 still takes that path itself, but only by deprecation, and this
    is the conversion it would do. A directory is an OpenSSL CA path -- hashed
    symlinks rather than one file -- so it is loaded as one.
    """

    if not isinstance(verify, str):
        return verify
    if os.path.isdir(verify):
        return ssl.create_default_context(capath=verify)
    return ssl.create_default_context(cafile=verify)


def _checked_timeout(timeout: float) -> float:
    """Return ``timeout`` if it can bound a request, else raise :class:`ValueError`.

    Nothing here accepts None, which httpx2 reads as "wait forever": a request
    that never comes back leaves a command, and the daemon serving it, with no
    way out. A long wait is asked for as a large number of seconds.
    """

    if timeout <= 0:
        raise ValueError(f"timeout must be greater than 0, not {timeout}")
    return timeout


def _default_client(base_url: str, verify: bool | str, *, timeout: float) -> httpx2.Client:
    """Build the httpx2 client used for one host.

    The session binds ``timeout`` before it holds on to this, so what it calls
    still has the ``(base_url, verify)`` shape a caller-supplied
    ``client_factory`` is written to.
    """

    return httpx2.Client(base_url=base_url, verify=_ssl_context(verify), timeout=timeout)


def _default_async_client(
    base_url: str, verify: bool | str, *, timeout: float
) -> httpx2.AsyncClient:
    """Build the awaited httpx2 client used for one host.

    The same settings as :func:`_default_client`, because the request a caller
    awaits must be the request a caller sends.
    """

    return httpx2.AsyncClient(base_url=base_url, verify=_ssl_context(verify), timeout=timeout)


def _extract_error(data: Any) -> tuple[str | None, str | None] | None:
    """Return ``(code, text)`` if ``data`` is an APIC error envelope, else None."""

    if not isinstance(data, dict):
        return None
    imdata = data.get("imdata")
    if not isinstance(imdata, list):
        return None
    for mo in imdata:
        if isinstance(mo, dict) and "error" in mo:
            attrs = mo["error"].get("attributes", {})
            return attrs.get("code"), attrs.get("text")
    return None


class _SessionBase(Generic[ClientT]):
    """What an authentication is, apart from how its requests travel.

    Everything here is decided without sending anything: which token is held,
    whether it is due for a refresh or over, what body a login and a logout
    carry, and what an APIC response means. :class:`Session` and
    :class:`AsyncSession` add only the sending.
    """

    # Assigned by the subclass, which is where the client's type is settled.
    _client: ClientT
    _client_factory: Callable[[str, bool | str], ClientT]

    def __init__(
        self,
        host: str,
        *,
        verify: bool | str = True,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = normalize_base_url(host)
        self.user: str | None = None
        self.refresh_timeout = DEFAULT_REFRESH_TIMEOUT
        self.timeout = _checked_timeout(timeout)
        self._clock = clock
        self._token: str | None = None
        self._last_auth = 0.0
        self._verify = verify
        # Node base URL -> client, most recently used last.
        self._node_clients: OrderedDict[str, ClientT] = OrderedDict()

    @property
    def logged_in(self) -> bool:
        return self._token is not None

    # -- lazy-refresh timing ---------------------------------------------

    def seconds_since_auth(self) -> float:
        return self._clock() - self._last_auth

    def is_expired(self) -> bool:
        return self.logged_in and self.seconds_since_auth() >= self.refresh_timeout

    def needs_refresh(self) -> bool:
        elapsed = self.seconds_since_auth()
        return self.logged_in and self.refresh_timeout / 2 <= elapsed < self.refresh_timeout

    def expires_at(self) -> float:
        """Clock value at which the session expires without further activity."""

        return self._last_auth + self.refresh_timeout

    def _refresh_due(self) -> bool:
        """Say whether a refresh is due, ruling out the two cases where it is not a question.

        Dropping what a session found over was holding is the caller's, which
        catches the exception to do it: closing an awaited client is itself
        awaited, and this decision is not.
        """

        if not self.logged_in:
            raise NotLoggedInError("not logged in")
        if self.is_expired():
            raise SessionExpiredError("session expired; please login again")
        return self.needs_refresh()

    # -- request bodies ---------------------------------------------------

    def _login_body(self, user: str, password: str) -> dict[str, Any]:
        return {"aaaUser": {"attributes": {"name": user, "pwd": password}}}

    def _logout_body(self) -> dict[str, Any]:
        return {"aaaUser": {"attributes": {"name": self.user}}}

    # -- the token --------------------------------------------------------

    def _forget_token(self) -> None:
        """Forget the token itself. The node clients holding a copy are the subclass's."""

        self._token = None
        self._client.cookies.clear()

    def _store_token(self, attrs: dict[str, Any]) -> None:
        self._token = attrs["token"]
        self.refresh_timeout = float(attrs.get("refreshTimeoutSeconds", self.refresh_timeout))
        self._last_auth = self._clock()
        self._client.cookies.set("APIC-cookie", self._token)

    # -- node clients -----------------------------------------------------

    def _take_node_client(self, host: str) -> tuple[ClientT, list[ClientT]]:
        """Return the client for ``host``, and the ones the LRU has just evicted.

        The token is stamped on every call rather than at refresh time, so a
        refreshed token reaches every node without a fan-out over the clients
        this session happens to be holding. Closing what was evicted is the
        caller's, for the reason :meth:`_refresh_due` gives.
        """

        base_url = normalize_base_url(host)
        client = self._node_clients.get(base_url)
        if client is None:
            client = self._client_factory(base_url, self._verify)
            self._node_clients[base_url] = client
        self._node_clients.move_to_end(base_url)
        evicted: list[ClientT] = []
        while len(self._node_clients) > NODE_CLIENT_MAX:
            _, old = self._node_clients.popitem(last=False)
            evicted.append(old)
        assert self._token is not None  # ensure_fresh() guarantees this
        client.cookies.set("APIC-cookie", self._token)
        return client, evicted

    # -- responses --------------------------------------------------------

    def _parse_login(self, resp: httpx2.Response) -> dict[str, Any]:
        data = self._parse(resp, action="login")
        try:
            return data["imdata"][0]["aaaLogin"]["attributes"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ApicError("unexpected login response", status=resp.status_code) from exc

    def _parse(self, resp: httpx2.Response, action: str = "request") -> Any:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if data is not None:
            err = _extract_error(data)
            if err is not None:
                code, text = err
                raise ApicError(text or f"APIC error {code}", code=code, status=resp.status_code)
        if resp.status_code >= _HTTP_ERROR_STATUS:
            raise ApicError(f"HTTP {resp.status_code} on {action}", status=resp.status_code)
        if data is None:
            raise ApicError("non-JSON response from APIC", status=resp.status_code)
        return data


class Session(_SessionBase[httpx2.Client]):
    """One authentication against an APIC, usable against fabric nodes too.

    The APIC owns the token: login, refresh and expiry are always evaluated
    against ``base_url``. Fabric switches accept that same token, so requests may
    be directed at a node instead, over a separate client that carries the very
    same ``APIC-cookie``. ``timeout`` bounds every one of those requests, the
    node's as much as the APIC's.

    ``client`` and ``client_factory`` are the way in for a client this
    constructor cannot express. Either one settles its own timeout, so ``timeout``
    does not reach it: what was handed over is used as it was handed over.
    """

    def __init__(
        self,
        host: str,
        *,
        verify: bool | str = True,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        client: httpx2.Client | None = None,
        client_factory: Callable[[str, bool | str], httpx2.Client] | None = None,
    ) -> None:
        super().__init__(host, verify=verify, timeout=timeout, clock=clock)
        self._client_factory = client_factory or partial(_default_client, timeout=self.timeout)
        self._client = client or self._client_factory(self.base_url, verify)

    # -- authentication ---------------------------------------------------

    def login(self, user: str, password: str) -> None:
        resp = self._send(
            self._client, "POST", "/api/aaaLogin.json", json=self._login_body(user, password)
        )
        self._store_token(self._parse_login(resp))
        self.user = user

    def refresh(self, *, timeout: float | None = None) -> None:
        if not self.logged_in:
            raise NotLoggedInError("not logged in")
        resp = self._send(self._client, "GET", "/api/aaaRefresh.json", timeout=timeout)
        self._store_token(self._parse_login(resp))

    def logout(self) -> None:
        """End the session on the APIC, then drop the token here.

        The token is dropped either way: an APIC that cannot be reached, or that
        refuses the request, still leaves this process logged out, and the
        failure is raised afterwards for the caller to report as it sees fit. An
        expired token has nothing left to end, so nothing is sent for one.
        """

        try:
            if self.logged_in and not self.is_expired():
                resp = self._send(
                    self._client,
                    "POST",
                    "/api/aaaLogout.json",
                    json=self._logout_body(),
                    timeout=LOGOUT_TIMEOUT,
                )
                self._parse(resp, action="logout")
        finally:
            self._discard()

    def _discard(self) -> None:
        """Forget the token without telling the APIC. See :meth:`logout`."""

        self._forget_token()
        self._drop_node_clients()

    def close(self) -> None:
        self._client.close()
        self._drop_node_clients()

    def ensure_fresh(self, *, timeout: float | None = None) -> None:
        """Refresh if past half-life, or fail if fully expired."""

        try:
            due = self._refresh_due()
        except SessionExpiredError:
            self._discard()
            raise
        if due:
            self.refresh(timeout=timeout)

    # -- requests ---------------------------------------------------------

    def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        host: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """GET from the APIC, or from a fabric node when ``host`` is given.

        ``timeout`` bounds this one call, refresh included, for a caller that
        cannot afford to wait even the session's own -- a lookup with a person
        waiting on it, say. Without it the session's timeout applies.
        """

        self.ensure_fresh(timeout=timeout)
        client = self._client if host is None else self._node_client(host)
        resp = self._send(client, "GET", path, params=params, timeout=timeout)
        return self._parse(resp)

    def post(self, path: str, body: str) -> Any:
        self.ensure_fresh()
        resp = self._send(
            self._client,
            "POST",
            path,
            content=body,
            headers={"Content-Type": "application/json"},
        )
        return self._parse(resp)

    # -- internals --------------------------------------------------------

    def _node_client(self, host: str) -> httpx2.Client:
        """Return the client for ``host``, carrying the current APIC token."""

        client, evicted = self._take_node_client(host)
        for old in evicted:
            old.close()
        return client

    def _drop_node_clients(self) -> None:
        for client in self._node_clients.values():
            client.close()
        self._node_clients.clear()

    def _send(
        self,
        client: httpx2.Client,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx2.Response:
        """Send a request, reporting which host was unreachable on failure.

        A ``timeout`` of None is not passed on at all: httpx2 reads that as "wait
        forever", where what is meant is the client's own timeout.
        """

        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            return client.request(method, path, **kwargs)
        except httpx2.RequestError as exc:
            raise ApicError(f"cannot reach {client.base_url}: {exc}") from exc


class AsyncSession(_SessionBase[httpx2.AsyncClient]):
    """:class:`Session`, awaited.

    The same authentication against the same APIC, sending the same requests in
    the same order and raising the same exceptions at the same points. Only the
    sending is awaited, so a caller keeps its event loop while the APIC thinks.
    """

    def __init__(
        self,
        host: str,
        *,
        verify: bool | str = True,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        client: httpx2.AsyncClient | None = None,
        client_factory: Callable[[str, bool | str], httpx2.AsyncClient] | None = None,
    ) -> None:
        super().__init__(host, verify=verify, timeout=timeout, clock=clock)
        self._client_factory = client_factory or partial(
            _default_async_client, timeout=self.timeout
        )
        self._client = client or self._client_factory(self.base_url, verify)

    # -- authentication ---------------------------------------------------

    async def login(self, user: str, password: str) -> None:
        resp = await self._send(
            self._client, "POST", "/api/aaaLogin.json", json=self._login_body(user, password)
        )
        self._store_token(self._parse_login(resp))
        self.user = user

    async def refresh(self, *, timeout: float | None = None) -> None:
        if not self.logged_in:
            raise NotLoggedInError("not logged in")
        resp = await self._send(self._client, "GET", "/api/aaaRefresh.json", timeout=timeout)
        self._store_token(self._parse_login(resp))

    async def logout(self) -> None:
        """End the session on the APIC, then drop the token here.

        As :meth:`Session.logout`: the token goes whether or not the APIC could
        be told, and the failure is raised afterwards.
        """

        try:
            if self.logged_in and not self.is_expired():
                resp = await self._send(
                    self._client,
                    "POST",
                    "/api/aaaLogout.json",
                    json=self._logout_body(),
                    timeout=LOGOUT_TIMEOUT,
                )
                self._parse(resp, action="logout")
        finally:
            await self._discard()

    async def _discard(self) -> None:
        """Forget the token without telling the APIC. See :meth:`logout`."""

        self._forget_token()
        await self._drop_node_clients()

    async def close(self) -> None:
        await self._client.aclose()
        await self._drop_node_clients()

    async def ensure_fresh(self, *, timeout: float | None = None) -> None:
        """Refresh if past half-life, or fail if fully expired."""

        try:
            due = self._refresh_due()
        except SessionExpiredError:
            await self._discard()
            raise
        if due:
            await self.refresh(timeout=timeout)

    # -- requests ---------------------------------------------------------

    async def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        host: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """GET from the APIC, or from a fabric node when ``host`` is given.

        ``timeout`` bounds the whole call, refresh included, as on
        :meth:`Session.get`.
        """

        await self.ensure_fresh(timeout=timeout)
        client = self._client if host is None else await self._node_client(host)
        resp = await self._send(client, "GET", path, params=params, timeout=timeout)
        return self._parse(resp)

    async def post(self, path: str, body: str) -> Any:
        await self.ensure_fresh()
        resp = await self._send(
            self._client,
            "POST",
            path,
            content=body,
            headers={"Content-Type": "application/json"},
        )
        return self._parse(resp)

    # -- internals --------------------------------------------------------

    async def _node_client(self, host: str) -> httpx2.AsyncClient:
        """Return the client for ``host``, carrying the current APIC token."""

        client, evicted = self._take_node_client(host)
        for old in evicted:
            await old.aclose()
        return client

    async def _drop_node_clients(self) -> None:
        for client in self._node_clients.values():
            await client.aclose()
        self._node_clients.clear()

    async def _send(
        self,
        client: httpx2.AsyncClient,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx2.Response:
        """Send a request, reporting which host was unreachable on failure.

        A ``timeout`` of None is not passed on at all, for the reason
        :meth:`Session._send` gives.
        """

        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            return await client.request(method, path, **kwargs)
        except httpx2.RequestError as exc:
            raise ApicError(f"cannot reach {client.base_url}: {exc}") from exc
