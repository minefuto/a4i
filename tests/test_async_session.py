"""What awaiting adds to a session, and nothing else.

tests/test_awaited.py checks that :class:`~a4i.session.AsyncSession` is
:class:`~a4i.session.Session` with the sending awaited, statement for statement.
The lazy-refresh timing, the logout rules, the node clients and the per-call
timeout are therefore not tested again here: they are not equivalent to what
test_session.py covers, they are the same statements.

What is left is what that check cannot see: the constructor it exempts, the
awaited close, and that the awaited client underneath carries a request out and
an answer back.
"""

from __future__ import annotations

import httpx2
import pytest

from a4i.session import AsyncSession, Session
from apic_mock import Clock, make_async_session

# -- the constructor, which test_awaited.py exempts -------------------------


async def test_a_session_built_without_a_client_gets_an_awaited_one() -> None:
    """The one line the two sessions cannot share, and the claim it makes.

    _default_async_client exists to agree with _default_client on everything but
    the awaiting -- "the request a caller awaits must be the request a caller
    sends" -- and nothing else would notice if it stopped.
    """

    session = AsyncSession("apic1.example.com", verify=False)
    synchronous = Session("apic1.example.com", verify=False)
    try:
        assert isinstance(session._client, httpx2.AsyncClient)
        assert str(session._client.base_url) == "https://apic1.example.com"
        assert session._client.timeout == synchronous._client.timeout
    finally:
        synchronous.close()
        await session.close()


async def test_an_awaited_session_takes_the_timeout_the_same_way() -> None:
    """A timeout given to either reaches the client the same, for the same reason."""

    session = AsyncSession("apic1.example.com", verify=False, timeout=120.0)
    synchronous = Session("apic1.example.com", verify=False, timeout=120.0)
    try:
        assert session._client.timeout == synchronous._client.timeout == httpx2.Timeout(120.0)
    finally:
        synchronous.close()
        await session.close()


# -- the awaited client carries a request out and an answer back ------------


async def test_an_awaited_login_holds_the_token() -> None:
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    assert session.logged_in
    assert session._token == "tok1"
    await session.close()


async def test_an_awaited_get_comes_back_with_the_answer() -> None:
    session = make_async_session({}, Clock())
    await session.login("admin", "pw")
    data = await session.get("/api/class/fvTenant.json")
    assert data["imdata"][0]["fvTenant"]["attributes"]["name"] == "common"
    await session.close()


async def test_an_awaited_post_sends_the_body_as_given() -> None:
    # The other verb: a different call into the awaited httpx2 client.
    state: dict = {}
    session = make_async_session(state, Clock())
    await session.login("admin", "pw")
    await session.post("/api/mo/uni/tn-demo.json", '{"fvTenant":{}}')
    assert state["last_method"] == "POST"
    assert state["last_body"] == '{"fvTenant":{}}'
    await session.close()


# -- the awaited close ------------------------------------------------------


async def test_close_leaves_no_client_open() -> None:
    """An awaited client left unclosed is a warning at collection time, and a leak.

    It is also where "aclose" is shown to be what httpx2 really calls it:
    test_awaited.py normalises that name away when it compares the two sessions,
    and this is where the assumption is paid for.
    """

    session = make_async_session({}, Clock())
    await session.login("admin", "pw")
    await session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    node = session._node_clients["https://leaf101.test"]
    await session.close()
    assert session._client.is_closed
    assert node.is_closed
    assert session._node_clients == {}


# -- an awaited failure is raised where the synchronous one is --------------


async def test_an_awaited_request_raises_what_the_apic_said(monkeypatch) -> None:
    # The awaited path has its own _send, so the one place it turns an httpx2
    # failure into an ApicError is worth crossing once here.
    from a4i.errors import ApicError

    session = make_async_session({"unreachable": {"apic.test"}}, Clock())
    with pytest.raises(ApicError) as exc:
        await session.login("admin", "pw")
    assert "cannot reach https://apic.test" in str(exc.value)
    await session.close()
