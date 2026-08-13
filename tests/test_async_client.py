"""What awaiting adds, and nothing else.

tests/test_awaited.py checks that :class:`~a4i.client.AsyncClient` is
:class:`~a4i.client.Client` with the sending awaited -- statement for statement,
signature for signature. So the parameter mapping, the dry-run comparison and the
fabric comparison are not tested again here: they are not merely equivalent to
the ones tests/test_client.py covers, they are the same statements.

What is left is what that check cannot see:

* the constructor, which it exempts because it is the one place the two really
  do differ -- httpx2's awaited client on this side, its synchronous one on the
  other;
* the asynchronous context manager, which is a protocol rather than a body, and
  which is also where "aclose" is shown to be what httpx2 really calls it -- the
  check normalises that name away on the strength of it;
* the awaited stack underneath -- AsyncDirectTransport over an AsyncSession over
  an awaited httpx2 client -- which has to carry a request out and an answer
  back.
"""

from __future__ import annotations

import pytest

from a4i.client import AsyncClient
from a4i.transport import AsyncDirectTransport
from apic_mock import Clock, make_async_session


@pytest.fixture
def state() -> dict:
    return {}


@pytest.fixture
async def client(state):
    """A logged-in client talking to the mocked APIC."""

    client = AsyncClient(transport=AsyncDirectTransport(make_async_session(state, Clock())))
    await client.login("admin", "pw")
    yield client
    await client.close()


# -- the awaited stack carries a request out and an answer back -------------


async def test_an_awaited_get_reaches_the_apic_and_comes_back(client) -> None:
    data = await client.get("fvTenant", kind="class")
    assert data["totalCount"] == "1"
    assert data["imdata"][0]["fvTenant"]["attributes"]["name"] == "common"


async def test_an_awaited_post_sends_the_body(client, state) -> None:
    # The other verb: a different call into the awaited httpx2 client.
    body = '{"fvTenant": {"attributes": {"name": "demo"}}}'
    await client.post("uni/tn-demo", body, kind="mo")
    assert state["last_method"] == "POST"
    assert state["last_body"] == body


# -- the constructor, which test_awaited.py exempts -------------------------


async def test_a_client_needs_a_host_or_a_transport() -> None:
    with pytest.raises(TypeError):
        AsyncClient()


async def test_a_client_built_from_a_host_owns_an_awaited_session() -> None:
    client = AsyncClient("apic1.example.com", verify=False)
    assert client._session is not None
    assert client._session.base_url == "https://apic1.example.com"
    await client.close()


async def test_an_awaited_client_hands_the_timeout_to_its_session() -> None:
    client = AsyncClient("apic1.example.com", verify=False, timeout=120.0)
    assert client._session is not None
    assert client._session.timeout == 120.0
    await client.close()


async def test_a_client_without_a_session_of_its_own_cannot_log_in() -> None:
    class Nowhere:
        async def get(self, target, kind, params, node): ...

        async def post(self, target, kind, body): ...

    client = AsyncClient(transport=Nowhere())
    with pytest.raises(TypeError):
        await client.login("admin", "pw")
    # Closing is still safe: there is nothing to close.
    await client.close()


# -- the asynchronous context manager ---------------------------------------


async def test_the_context_manager_closes_the_session(state) -> None:
    """``async with`` reaches __aenter__ and __aexit__, and the close lands.

    The close landing is what shows httpx2 spells its awaited close "aclose":
    test_awaited.py takes that name for granted when it compares the two
    classes, and this is where the assumption is paid for.
    """

    session = make_async_session(state, Clock())
    async with AsyncClient(transport=AsyncDirectTransport(session)) as client:
        await client.login("admin", "pw")
        assert client.logged_in
    with pytest.raises(RuntimeError):
        await client.get("fvTenant", kind="class")


# -- the package's public names --------------------------------------------


def test_the_public_names_resolve_without_importing_httpx2_up_front() -> None:
    import a4i

    assert a4i.AsyncClient is AsyncClient
