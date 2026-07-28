"""The awaited library entry point: the same requests, awaited.

The parameter mapping, the dry-run comparison and the fabric comparison are the
ones test_client.py already covers, because both clients call the very same
functions for them. What is tested here is that awaiting a call sends what
calling it sends -- the same paths, the same parameters, the same bodies, in the
same order -- and that a client closes what it opened.
"""

from __future__ import annotations

import json

import pytest

from a4i import mo as mo_
from a4i.client import AsyncClient
from a4i.errors import ApicError, NotLoggedInError, SessionExpiredError
from a4i.transport import AsyncDirectTransport
from apic_mock import APIC_HOST, Clock, make_async_session


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


# -- get -------------------------------------------------------------------


async def test_get_returns_the_response_as_it_came(client) -> None:
    data = await client.get("fvTenant", kind="class")
    # Not just imdata: totalCount is what a paginating caller needs.
    assert data["totalCount"] == "1"
    assert data["imdata"][0]["fvTenant"]["attributes"]["name"] == "common"


async def test_get_sends_no_parameters_by_default(client, state) -> None:
    await client.get("fvTenant", kind="class")
    assert state["last_params"] == {}


async def test_get_maps_every_option_to_its_aci_parameter(client, state) -> None:
    await client.get(
        "fvTenant",
        kind="class",
        query_target="subtree",
        target_subtree_class="fvAEPg,fvBD",
        query_target_filter='eq(fvTenant.name,"common")',
        rsp_subtree="full",
        rsp_subtree_class="fvRsPathAtt",
        rsp_subtree_filter='gt(fvAEPg.prio,"1")',
        rsp_subtree_include="faults,no-scoped",
        rsp_prop_include="config-only",
        order_by="fvTenant.name|desc",
        page=0,
        page_size=10,
    )
    assert state["last_params"] == {
        "query-target": "subtree",
        "target-subtree-class": "fvAEPg,fvBD",
        "query-target-filter": 'eq(fvTenant.name,"common")',
        "rsp-subtree": "full",
        "rsp-subtree-class": "fvRsPathAtt",
        "rsp-subtree-filter": 'gt(fvAEPg.prio,"1")',
        "rsp-subtree-include": "faults,no-scoped",
        "rsp-prop-include": "config-only",
        "order-by": "fvTenant.name|desc",
        "page": "0",
        "page-size": "10",
    }


async def test_get_rejects_an_invalid_value_before_sending(client, state) -> None:
    with pytest.raises(ValueError) as exc:
        await client.get("fvTenant", kind="class", query_target="nope")
    assert "invalid query_target" in str(exc.value)
    assert "last_params" not in state


async def test_get_builds_a_class_or_an_mo_path(client, state) -> None:
    await client.get("uni/tn-common", kind="mo")
    assert state["mo_requests"] == {"/api/mo/uni/tn-common.json": 1}


async def test_get_reaches_a_fabric_node_with_the_same_token(client, state) -> None:
    data = await client.get("l1PhysIf", kind="class", node="leaf101.example.com")
    assert data["imdata"][0]["l1PhysIf"]["attributes"]["id"] == "eth1/1"
    assert state["node_requests"] == [("leaf101.example.com", "/api/class/l1PhysIf.json")]
    assert "tok1" in state["node_cookie"]


async def test_get_reports_an_apic_error(client) -> None:
    with pytest.raises(ApicError) as exc:
        await client.get("boom", kind="class")
    assert exc.value.code == "122"


async def test_get_before_login_does_not_reach_the_apic(state) -> None:
    client = AsyncClient(transport=AsyncDirectTransport(make_async_session(state, Clock())))
    with pytest.raises(NotLoggedInError):
        await client.get("fvTenant", kind="class")
    await client.close()


# -- post ------------------------------------------------------------------


async def test_post_sends_text_exactly_as_given(client, state) -> None:
    body = '{"fvTenant": {"attributes": {"name": "demo"}}}'
    await client.post("uni/tn-demo", body, kind="mo")
    assert state["last_method"] == "POST"
    assert state["last_body"] == body


async def test_post_serializes_an_object(client, state) -> None:
    await client.post("uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo")
    assert json.loads(state["last_body"]) == {"fvTenant": {"attributes": {"name": "demo"}}}


@pytest.mark.parametrize(("body", "message"), [("", "empty POST body"), ("{no", "invalid JSON")])
async def test_post_rejects_a_body_it_cannot_read(client, state, body, message) -> None:
    with pytest.raises(ValueError) as exc:
        await client.post("uni/tn-demo", body, kind="mo")
    assert message in str(exc.value)
    assert "last_method" not in state


# -- dry_run (what a POST would change) ------------------------------------


async def test_dry_run_fetches_the_current_state_and_never_posts(client, state) -> None:
    changes = await client.dry_run(
        "uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo"
    )
    assert state["last_method"] == "GET"
    # The whole subtree, and only the settable properties.
    assert state["last_params"] == {"rsp-subtree": "full", "rsp-prop-include": "config-only"}
    assert [(c.kind, c.dn) for c in changes] == [("modified", "uni/tn-common")]
    assert changes[0].attributes == {"name": ("common", "demo")}


async def test_dry_run_walks_every_root_of_an_array_body(client, state) -> None:
    changes = await client.dry_run(
        "uni",
        [
            {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "a"}}},
            {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "b"}}},
        ],
        kind="mo",
    )
    assert state["mo_requests"] == {"/api/mo/uni/tn-demo.json": 2}
    assert [c.kind for c in changes] == ["modified", "modified"]


async def test_dry_run_needs_a_dn_it_can_work_out(client, state) -> None:
    with pytest.raises(ValueError) as exc:
        await client.dry_run("fvTenant", {"fvTenant": {"attributes": {"name": "x"}}}, kind="class")
    assert "cannot determine the target DN" in str(exc.value)
    assert state["mo_requests"] == {}


async def test_post_with_dry_run_compares_instead_of_sending(client, state) -> None:
    body = {"fvTenant": {"attributes": {"name": "demo"}}}
    changes = await client.post("uni/tn-demo", body, kind="mo", dry_run=True)
    assert isinstance(changes[0], mo_.Change)
    assert state["last_method"] == "GET"


# -- diff (the fabric against an intended configuration) --------------------


async def test_diff_fetches_one_subtree_per_top_level_mo_and_never_writes(client, state) -> None:
    await client.diff()
    assert state["last_method"] == "GET"
    # uni is listed once, then each of its children is fetched whole, one
    # request at a time -- the order the synchronous client makes them in.
    assert state["mo_requests"] == {
        "/api/mo/uni.json": 1,
        "/api/mo/uni/tn-common.json": 1,
        "/api/mo/uni/tn-infra.json": 1,
    }
    assert state["last_params"] == {"rsp-subtree": "full", "rsp-prop-include": "config-only"}


async def test_diff_reports_everything_the_configuration_leaves_out(client) -> None:
    changes = await client.diff()
    assert [(c.kind, c.dn) for c in changes] == [
        ("extra", "uni/tn-common/BD-default"),
        ("extra", "uni/tn-common/ap-web"),
        ("extra", "uni/tn-infra"),
    ]


async def test_diff_is_empty_when_the_configuration_describes_the_fabric(client) -> None:
    changes = await client.diff(
        {"fvAp": {"attributes": {"dn": "uni/tn-common/ap-web"}}},
        {"fvBD": {"attributes": {"dn": "uni/tn-common/BD-default"}}},
        {"fvTenant": {"attributes": {"dn": "uni/tn-infra", "name": "infra"}}},
    )
    assert changes == []


async def test_diff_leaves_an_excluded_subtree_out_of_the_report(client) -> None:
    changes = await client.diff(exclude="uni/tn-common")
    assert [(c.kind, c.dn) for c in changes] == [("extra", "uni/tn-infra")]


async def test_diff_names_the_subtree_it_could_not_fetch(client, state) -> None:
    state["fail_path"] = "/api/mo/uni/tn-infra.json"
    with pytest.raises(ApicError) as exc:
        await client.diff()
    assert "uni/tn-infra" in str(exc.value)
    assert "forbidden" in str(exc.value)


async def test_diff_refuses_a_paged_answer_rather_than_reporting_it_as_a_diff() -> None:
    # The APIC says uni has three children and hands back two. Comparing the
    # two would report everything under the third as missing from a fabric that
    # is carrying it, so the fetch fails instead.
    class ShortTransport:
        async def get(self, target, kind, params, node):
            return {
                "totalCount": "3",
                "imdata": [
                    {"fvTenant": {"attributes": {"dn": "uni/tn-common"}}},
                    {"fvTenant": {"attributes": {"dn": "uni/tn-infra"}}},
                ],
            }

        async def post(self, target, kind, body):  # pragma: no cover - diff never posts
            raise AssertionError("diff writes nothing")

    with pytest.raises(ApicError) as exc:
        await AsyncClient(transport=ShortTransport()).diff()
    assert "uni: the APIC returned 2 of 3 MOs" in str(exc.value)


async def test_diff_does_not_walk_the_runtime_containers_under_uni(client, state) -> None:
    await client.diff()
    assert "/api/mo/uni/epp.json" not in state["mo_requests"]


# -- the session a client owns ---------------------------------------------


async def test_a_client_needs_a_host_or_a_transport() -> None:
    with pytest.raises(TypeError):
        AsyncClient()


async def test_a_client_without_a_session_of_its_own_cannot_log_in() -> None:
    class Nowhere:
        async def get(self, target, kind, params, node): ...

        async def post(self, target, kind, body): ...

    client = AsyncClient(transport=Nowhere())
    with pytest.raises(TypeError):
        await client.login("admin", "pw")
    # Closing is still safe: there is nothing to close.
    await client.close()


async def test_the_context_manager_closes_the_session(state) -> None:
    session = make_async_session(state, Clock())
    async with AsyncClient(transport=AsyncDirectTransport(session)) as client:
        await client.login("admin", "pw")
        assert client.logged_in
    with pytest.raises(RuntimeError):
        await client.get("fvTenant", kind="class")


async def test_the_token_is_refreshed_past_its_half_life(state) -> None:
    clock = Clock()
    client = AsyncClient(transport=AsyncDirectTransport(make_async_session(state, clock)))
    await client.login("admin", "pw")
    clock.advance(301)
    await client.get("fvTenant", kind="class")
    assert state["token_n"] == 2
    assert "tok2" in state["last_cookie"]
    await client.close()


async def test_an_expired_token_asks_for_a_new_login(state) -> None:
    clock = Clock()
    client = AsyncClient(transport=AsyncDirectTransport(make_async_session(state, clock)))
    await client.login("admin", "pw")
    clock.advance(601)
    with pytest.raises(SessionExpiredError):
        await client.get("fvTenant", kind="class")
    await client.close()


async def test_the_apic_host_is_normalized(state) -> None:
    client = AsyncClient(transport=AsyncDirectTransport(make_async_session(state, Clock())))
    assert client._session is not None
    assert client._session.base_url == f"https://{APIC_HOST}"
    await client.close()


async def test_a_client_built_from_a_host_owns_an_awaited_session() -> None:
    client = AsyncClient("apic1.example.com", verify=False)
    assert client._session is not None
    assert client._session.base_url == "https://apic1.example.com"
    await client.close()


# -- the package's public names --------------------------------------------


def test_the_public_names_resolve_without_importing_httpx2_up_front() -> None:
    import a4i

    assert a4i.AsyncClient is AsyncClient
