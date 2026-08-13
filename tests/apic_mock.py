"""Shared test helpers: a fake clock and a mocked APIC transport."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx2

from a4i.session import DEFAULT_TIMEOUT, AsyncSession, Session


class Clock:
    """A manually advanced monotonic clock for deterministic timing tests."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


APIC_HOST = "apic.test"


def _make_handler(state: dict[str, Any]) -> Callable[[httpx2.Request], httpx2.Response]:
    """Return the handler standing in for an APIC and the fabric nodes behind it.

    Requests are routed by host: ``apic.test`` behaves like an APIC, and any
    other host like a switch serving its local MIT read-only.

    ``state`` records interactions and toggles behaviour:
      - ``fail_login``: make aaaLogin return a 401 error MO
      - ``fail_logout``: make aaaLogout return a 403 error MO
      - ``fail_path``: make this one request path return a 403 error MO
      - ``logouts``: ``(cookie header, raw body)`` of every aaaLogout received
      - ``token_n``: incremented per issued token (login/refresh)
      - ``last_cookie``: APIC-cookie header seen on the last data request
      - ``mo_requests``: per-path count of /api/mo/... requests
      - ``last_path``: path of the last data request
      - ``last_params``: query parameters of the last data request
      - ``last_method`` / ``last_body``: method and raw body of the last data request
      - ``unreachable``: hosts whose requests raise a connection error
      - ``node_requests``: ``(host, path)`` of every request that reached a node
      - ``node_params``: query parameters of the last node request
      - ``timeouts``: the read timeout in force on each request, in order
      - ``node_cookie``: cookie header seen on the last node request
      - ``verify``: per base URL, the TLS verification the client was built with
    """

    state.setdefault("token_n", 0)
    state.setdefault("mo_requests", {})
    state.setdefault("unreachable", set())
    state.setdefault("node_requests", [])
    state.setdefault("logouts", [])

    def issue_token() -> httpx2.Response:
        state["token_n"] += 1
        return httpx2.Response(
            200,
            json={
                "imdata": [
                    {
                        "aaaLogin": {
                            "attributes": {
                                "token": f"tok{state['token_n']}",
                                "refreshTimeoutSeconds": "600",
                            }
                        }
                    }
                ]
            },
        )

    def node_handler(request: httpx2.Request) -> httpx2.Response:
        """A fabric switch: same REST surface, but it issues no tokens of its own."""

        path = request.url.path
        state["node_requests"].append((request.url.host, path))
        state["node_cookie"] = request.headers.get("cookie")
        state["node_params"] = dict(request.url.params)
        if path in ("/api/aaaLogin.json", "/api/aaaRefresh.json"):
            return httpx2.Response(
                403,
                json={"imdata": [{"error": {"attributes": {"code": "403", "text": "no auth"}}}]},
            )
        if path == "/api/mo/sys.json":
            # The switch's own MIT: a tree the APIC does not have.
            return httpx2.Response(
                200,
                json={
                    "totalCount": "2",
                    "imdata": [
                        {"l1PhysIf": {"attributes": {"dn": "sys/phys-[eth1/1]"}}},
                        {"l1PhysIf": {"attributes": {"dn": "sys/phys-[eth1/2]"}}},
                    ],
                },
            )
        if path == "/api/mo/uni.json":
            # Same DN as on the APIC, different children: only the tenants the
            # switch has resolved policy for.
            return httpx2.Response(
                200,
                json={
                    "totalCount": "1",
                    "imdata": [{"fvTenant": {"attributes": {"dn": "uni/tn-infra"}}}],
                },
            )
        return httpx2.Response(
            200,
            json={
                "totalCount": "1",
                "imdata": [{"l1PhysIf": {"attributes": {"id": "eth1/1", "adminSt": "up"}}}],
            },
        )

    def handler(request: httpx2.Request) -> httpx2.Response:
        # httpx2 resolves the effective timeout per request, so this is what the
        # caller asked for, or the client default when it asked for nothing.
        state["timeouts"] = state.setdefault("timeouts", []) + [
            request.extensions.get("timeout", {}).get("read")
        ]
        if request.url.host in state["unreachable"]:
            raise httpx2.ConnectError("connection refused", request=request)
        if request.url.host != APIC_HOST:
            return node_handler(request)
        path = request.url.path
        if path == "/api/aaaLogin.json":
            if state.get("fail_login"):
                return httpx2.Response(
                    401,
                    json={
                        "imdata": [
                            {
                                "error": {
                                    "attributes": {"code": "401", "text": "Invalid credentials"}
                                }
                            }
                        ]
                    },
                )
            return issue_token()
        if path == "/api/aaaRefresh.json":
            return issue_token()
        if path == "/api/aaaLogout.json":
            state["logouts"].append((request.headers.get("cookie"), request.content.decode()))
            if state.get("fail_logout"):
                return httpx2.Response(
                    403,
                    json={
                        "imdata": [
                            {"error": {"attributes": {"code": "403", "text": "logout refused"}}}
                        ]
                    },
                )
            return httpx2.Response(200, json={"totalCount": "0", "imdata": []})

        state["last_cookie"] = request.headers.get("cookie")
        state["last_path"] = path
        params = dict(request.url.params)
        state["last_params"] = params
        state["last_method"] = request.method
        state["last_body"] = request.content.decode()
        if path.startswith("/api/mo/"):
            counts = state["mo_requests"]
            counts[path] = counts.get(path, 0) + 1
        if path == state.get("fail_path"):
            return httpx2.Response(
                403,
                json={"imdata": [{"error": {"attributes": {"code": "403", "text": "forbidden"}}}]},
            )
        if path == "/api/mo/uni.json":
            children = [
                {"fvTenant": {"attributes": {"dn": "uni/tn-common"}}},
                {"fvTenant": {"attributes": {"dn": "uni/tn-infra"}}},
            ]
            if params.get("rsp-prop-include") != "config-only":
                # uni carries runtime containers as well, and only "config-only"
                # leaves them out. Nobody configured this one and no intended
                # configuration will name it.
                children.append({"eppInst": {"attributes": {"dn": "uni/epp"}}})
            return httpx2.Response(
                200,
                json={"totalCount": str(len(children)), "imdata": children},
            )
        if path == "/api/mo/uni/tn-common.json":
            return httpx2.Response(
                200,
                json={
                    "totalCount": "2",
                    "imdata": [
                        {"fvAp": {"attributes": {"dn": "uni/tn-common/ap-web"}}},
                        {"fvBD": {"attributes": {"dn": "uni/tn-common/BD-default"}}},
                    ],
                },
            )
        if path == "/api/mo/uni/tn-infra.json":
            return httpx2.Response(
                200,
                json={
                    "totalCount": "1",
                    "imdata": [
                        {"fvTenant": {"attributes": {"dn": "uni/tn-infra", "name": "infra"}}}
                    ],
                },
            )
        if path == "/api/class/boom.json":
            return httpx2.Response(
                400,
                json={
                    "imdata": [{"error": {"attributes": {"code": "122", "text": "boom failed"}}}]
                },
            )
        return httpx2.Response(
            200,
            json={
                "totalCount": "1",
                "imdata": [{"fvTenant": {"attributes": {"name": "common", "dn": "uni/tn-common"}}}],
            },
        )

    return handler


def make_client(state: dict[str, Any], base_url: str = f"https://{APIC_HOST}") -> httpx2.Client:
    """Return an httpx2.Client backed by a mocked APIC and mocked fabric nodes."""

    return httpx2.Client(transport=httpx2.MockTransport(_make_handler(state)), base_url=base_url)


def make_async_client(
    state: dict[str, Any], base_url: str = f"https://{APIC_HOST}"
) -> httpx2.AsyncClient:
    """The very same mocked fabric, behind an awaited client.

    httpx2.MockTransport serves a synchronous handler to an awaited client too,
    so the fabric an async test talks to is the one a sync test talks to, down
    to the recorded state. What the two see can then differ only where a4i does.
    """

    return httpx2.AsyncClient(
        transport=httpx2.MockTransport(_make_handler(state)), base_url=base_url
    )


def make_client_factory(state: dict[str, Any]):
    """A Session-compatible client factory serving every host from the mock."""

    def factory(base_url: str, verify: bool | str = True) -> httpx2.Client:
        state.setdefault("verify", {})[base_url] = verify
        return make_client(state, base_url)

    return factory


def make_async_client_factory(state: dict[str, Any]):
    """An AsyncSession-compatible client factory serving every host from the mock."""

    def factory(base_url: str, verify: bool | str = True) -> httpx2.AsyncClient:
        state.setdefault("verify", {})[base_url] = verify
        return make_async_client(state, base_url)

    return factory


def make_session(state: dict[str, Any], clock: Clock, *, verify: bool | str = True) -> Session:
    return Session(APIC_HOST, verify=verify, client_factory=make_client_factory(state), clock=clock)


def make_async_session(
    state: dict[str, Any], clock: Clock, *, verify: bool | str = True
) -> AsyncSession:
    return AsyncSession(
        APIC_HOST, verify=verify, client_factory=make_async_client_factory(state), clock=clock
    )


def make_session_factory(state: dict[str, Any], clock: Clock):
    """A Daemon-compatible factory that serves every host from the mock.

    ``timeout`` is carried into the session, where the daemon reads it back to
    report it, but not into the mocked clients: a factory of one's own settles
    its own timeout, and this one serves a transport that never waits.
    """

    def factory(
        host: str, *, verify: bool | str = True, timeout: float = DEFAULT_TIMEOUT
    ) -> Session:
        return Session(
            host,
            verify=verify,
            timeout=timeout,
            client_factory=make_client_factory(state),
            clock=clock,
        )

    return factory
