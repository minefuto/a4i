"""The awaited session: the same requests, at the same points, awaited.

What a session decides without sending anything -- which token is held, whether
it is due for a refresh or over, what an APIC response means -- lives in the
base both sessions share and is covered by test_session.py. What is left, and
what is tested here, is the sending: that every request the synchronous session
makes is made here too, in the same order, and that awaiting a client does not
leave one open.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from a4i.session import (
    NODE_CLIENT_MAX,
    ApicError,
    NotLoggedInError,
    SessionExpiredError,
)
from apic_mock import Clock, make_async_session


async def test_login_stores_token_in_memory() -> None:
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    assert session.logged_in
    assert session.user == "admin"
    assert session.refresh_timeout == 600
    assert session._token == "tok1"
    await session.close()


async def test_login_failure_raises_with_apic_text() -> None:
    session = make_async_session({"fail_login": True}, Clock())
    with pytest.raises(ApicError) as exc:
        await session.login("admin", "bad")
    assert "Invalid credentials" in str(exc.value)
    assert not session.logged_in
    await session.close()


async def test_get_after_login() -> None:
    session = make_async_session({}, Clock())
    await session.login("admin", "pw")
    data = await session.get("/api/class/fvTenant.json")
    assert data["imdata"][0]["fvTenant"]["attributes"]["name"] == "common"
    await session.close()


async def test_request_requires_login() -> None:
    session = make_async_session({}, Clock())
    with pytest.raises(NotLoggedInError):
        await session.get("/api/class/fvTenant.json")
    await session.close()


async def test_error_response_raises() -> None:
    session = make_async_session({}, Clock())
    await session.login("admin", "pw")
    with pytest.raises(ApicError) as exc:
        await session.get("/api/class/boom.json")
    assert "boom failed" in str(exc.value)
    await session.close()


async def test_post_sends_the_body_as_given() -> None:
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    await session.post("/api/mo/uni/tn-demo.json", '{"fvTenant":{}}')
    assert state["last_method"] == "POST"
    assert state["last_body"] == '{"fvTenant":{}}'
    await session.close()


# -- the lazy refresh, as the synchronous session times it ------------------


async def test_refresh_updates_token() -> None:
    state: dict = {}
    clock = Clock()
    session = make_async_session(state, clock)
    await session.login("admin", "pw")  # tok1
    clock.advance(301)  # past half of 600 -> lazy refresh on next request
    await session.get("/api/class/fvTenant.json")
    assert state["last_cookie"] == "APIC-cookie=tok2"
    await session.close()


async def test_no_refresh_before_half_life() -> None:
    state: dict = {}
    clock = Clock()
    session = make_async_session(state, clock)
    await session.login("admin", "pw")  # tok1
    clock.advance(299)  # before half of 600
    await session.get("/api/class/fvTenant.json")
    assert state["last_cookie"] == "APIC-cookie=tok1"
    await session.close()


async def test_refresh_on_demand_requires_login() -> None:
    session = make_async_session({}, Clock())
    with pytest.raises(NotLoggedInError):
        await session.refresh()
    await session.close()


async def test_expired_session_requires_relogin() -> None:
    clock = Clock()
    session = make_async_session({}, clock)
    await session.login("admin", "pw")
    clock.advance(600)  # full lifetime elapsed with no activity
    with pytest.raises(SessionExpiredError):
        await session.get("/api/class/fvTenant.json")
    assert not session.logged_in
    await session.close()


async def test_an_expired_session_drops_the_node_clients_it_held() -> None:
    """Being told the session is over is the last thing that happens: it is dropped first."""

    state: dict = {}
    clock = Clock()
    session = make_async_session(state, clock)
    await session.login("admin", "pw")
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    clock.advance(600)
    with pytest.raises(SessionExpiredError):
        await session.get("/api/class/fvTenant.json")
    assert session._node_clients == {}
    await session.close()


# -- logout ----------------------------------------------------------------


async def test_logout_tells_the_apic() -> None:
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    await session.logout()
    assert len(state["logouts"]) == 1
    cookie, body = state["logouts"][0]
    assert cookie == "APIC-cookie=tok1"
    assert json.loads(body) == {"aaaUser": {"attributes": {"name": "admin"}}}
    assert not session.logged_in
    await session.close()


async def test_logout_without_a_token_sends_nothing() -> None:
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.logout()
    assert state["logouts"] == []
    await session.close()


async def test_expired_session_is_not_logged_out_on_the_apic() -> None:
    state: dict = {}
    clock = Clock()
    session = make_async_session(state, clock)
    await session.login("admin", "pw")
    clock.advance(600)  # full lifetime elapsed with no activity
    await session.logout()
    assert state["logouts"] == []
    assert not session.logged_in
    await session.close()


async def test_logout_drops_the_token_even_when_the_apic_refuses() -> None:
    state: dict = {"fail_logout": True}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    with pytest.raises(ApicError) as exc:
        await session.logout()
    assert "logout refused" in str(exc.value)
    assert not session.logged_in
    assert session._node_clients == {}
    await session.close()


async def test_logout_drops_the_token_when_the_apic_is_unreachable() -> None:
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    state["unreachable"] = {"apic.test"}
    with pytest.raises(ApicError) as exc:
        await session.logout()
    assert "cannot reach https://apic.test" in str(exc.value)
    assert not session.logged_in
    await session.close()


async def test_unreachable_host_names_the_destination() -> None:
    state: dict = {"unreachable": {"apic.test"}}
    session = make_async_session(state, Clock())
    with pytest.raises(ApicError) as exc:
        await session.login("admin", "pw")
    assert "cannot reach https://apic.test" in str(exc.value)
    await session.close()


# -- querying a fabric node with the APIC's token --------------------------


async def test_node_get_uses_the_apic_token() -> None:
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    data = await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert data["imdata"][0]["l1PhysIf"]["attributes"]["id"] == "eth1/1"
    assert state["node_requests"] == [("leaf101.test", "/api/class/l1PhysIf.json")]
    assert state["node_cookie"] == "APIC-cookie=tok1"
    # The APIC is untouched by a node query beyond the login itself.
    assert state["token_n"] == 1
    await session.close()


async def test_node_client_is_reused_per_host() -> None:
    session = make_async_session({}, Clock())
    await session.login("admin", "pw")
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    first = session._node_clients["https://leaf101.test"]
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert session._node_clients["https://leaf101.test"] is first
    await session.get("/api/class/l1PhysIf.json", host="leaf102.test")
    assert list(session._node_clients) == ["https://leaf101.test", "https://leaf102.test"]
    await session.close()


async def test_node_clients_inherit_the_login_tls_setting() -> None:
    state: dict = {}
    session = make_async_session(state, Clock(), verify=False)
    await session.login("admin", "pw")
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert state["verify"]["https://leaf101.test"] is False
    await session.close()


async def test_node_get_refreshes_against_the_apic() -> None:
    state: dict = {}
    clock = Clock()
    session = make_async_session(state, clock)
    await session.login("admin", "pw")  # tok1
    clock.advance(301)  # past half of 600
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert state["token_n"] == 2
    assert state["node_cookie"] == "APIC-cookie=tok2"
    await session.close()


async def test_node_get_on_expired_session_requires_relogin() -> None:
    state: dict = {}
    clock = Clock()
    session = make_async_session(state, clock)
    await session.login("admin", "pw")
    clock.advance(600)
    with pytest.raises(SessionExpiredError):
        await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert state["node_requests"] == []
    await session.close()


async def test_node_clients_are_evicted_and_closed() -> None:
    session = make_async_session({}, Clock())
    await session.login("admin", "pw")
    for n in range(NODE_CLIENT_MAX + 1):
        await session.get("/api/class/l1PhysIf.json", host=f"leaf{n}.test")
    assert len(session._node_clients) == NODE_CLIENT_MAX
    evicted = "https://leaf0.test"
    assert evicted not in session._node_clients
    await session.close()


async def test_logout_drops_node_clients() -> None:
    session = make_async_session({}, Clock())
    await session.login("admin", "pw")
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    await session.logout()
    assert session._node_clients == {}
    await session.close()


async def test_close_leaves_no_client_open() -> None:
    """An awaited client left unclosed is a warning at collection time, and a leak."""

    session = make_async_session({}, Clock())
    await session.login("admin", "pw")
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    node = session._node_clients["https://leaf101.test"]
    await session.close()
    assert session._client.is_closed
    assert node.is_closed
    assert session._node_clients == {}


# -- per-call timeout ------------------------------------------------------


async def test_get_uses_the_client_timeout_by_default() -> None:
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    await session.get("/api/class/fvTenant.json")
    assert state["timeouts"][-1] == httpx2.AsyncClient().timeout.read
    await session.close()


async def test_get_timeout_applies_to_the_call() -> None:
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    await session.get("/api/class/fvTenant.json", timeout=3.0)
    assert state["timeouts"][-1] == 3.0
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test", timeout=3.0)
    assert state["timeouts"][-1] == 3.0
    await session.close()


async def test_get_timeout_bounds_the_refresh_too() -> None:
    state: dict = {}
    clock = Clock()
    session = make_async_session(state, clock)
    await session.login("admin", "pw")
    clock.advance(301)  # past half of 600: the next get refreshes first
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test", timeout=3.0)
    assert state["token_n"] == 2
    # The refresh and the node query, both bounded.
    assert state["timeouts"][-2:] == [3.0, 3.0]
    await session.close()


async def test_unreachable_node_names_the_destination() -> None:
    state: dict = {"unreachable": {"leaf101.test"}}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    with pytest.raises(ApicError) as exc:
        await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert "cannot reach https://leaf101.test" in str(exc.value)
    await session.close()
