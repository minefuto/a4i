"""The library entry point: the same requests the CLI makes, without a daemon."""

from __future__ import annotations

import json

import pytest

from a4i import ipc
from a4i import mo as mo_
from a4i.client import Client
from a4i.errors import ApicError, NotLoggedInError, SessionExpiredError
from a4i.session import DEFAULT_TIMEOUT
from a4i.transport import DaemonTransport, DirectTransport
from apic_mock import APIC_HOST, Clock, make_session


@pytest.fixture
def state() -> dict:
    return {}


@pytest.fixture
def client(state) -> Client:
    """A logged-in client talking to the mocked APIC."""

    client = Client(transport=DirectTransport(make_session(state, Clock())))
    client.login("admin", "pw")
    return client


# -- get -------------------------------------------------------------------


def test_get_returns_the_response_as_it_came(client) -> None:
    data = client.get("fvTenant", kind="class")
    # Not just imdata: totalCount is what a paginating caller needs.
    assert data["totalCount"] == "1"
    assert data["imdata"][0]["fvTenant"]["attributes"]["name"] == "common"


def test_get_sends_no_parameters_by_default(client, state) -> None:
    client.get("fvTenant", kind="class")
    assert state["last_params"] == {}


def test_get_maps_every_option_to_its_aci_parameter(client, state) -> None:
    client.get(
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
    # The keyword argument names are the CLI option names with underscores, and
    # the URL order is the same one the CLI produces.
    assert list(state["last_params"]) == [
        "query-target",
        "target-subtree-class",
        "query-target-filter",
        "rsp-subtree",
        "rsp-subtree-class",
        "rsp-subtree-filter",
        "rsp-subtree-include",
        "rsp-prop-include",
        "order-by",
        "page",
        "page-size",
    ]


def test_get_sends_the_first_page(client, state) -> None:
    # page 0 is a real page, so it must survive a falsy-value filter.
    client.get("fvTenant", kind="class", page=0, page_size=100)
    assert state["last_params"] == {"page": "0", "page-size": "100"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query_target": "bogus"}, "invalid query_target: 'bogus'"),
        ({"rsp_subtree": "ful"}, "invalid rsp_subtree: 'ful'"),
        ({"rsp_prop_include": "bogus"}, "invalid rsp_prop_include: 'bogus'"),
        ({"rsp_subtree_include": "bogus"}, "invalid rsp_subtree_include value: bogus"),
        # Every element of the list is checked, not just the first.
        ({"rsp_subtree_include": "faults,bogus"}, "invalid rsp_subtree_include value: bogus"),
        ({"page": -1, "page_size": 10}, "page must be 0 or greater"),
        ({"page": 0, "page_size": 0}, "page_size must be 1 or greater"),
        ({"page": 1}, "page and page_size must be given together"),
        ({"page_size": 50}, "page and page_size must be given together"),
    ],
)
def test_get_rejects_invalid_values_before_sending(client, state, kwargs, message) -> None:
    with pytest.raises(ValueError) as exc:
        client.get("fvTenant", kind="class", **kwargs)
    assert message in str(exc.value)
    assert "last_params" not in state


@pytest.mark.parametrize("option", ["target_subtree_class", "rsp_subtree_class"])
def test_get_accepts_a_sequence_of_class_names(client, state, option) -> None:
    client.get("fvTenant", kind="class", **{option: ["fvAEPg", "fvBD"]})
    assert state["last_params"][option.replace("_", "-")] == "fvAEPg,fvBD"


def test_get_accepts_a_sequence_of_subtree_categories(client, state) -> None:
    client.get("fvTenant", kind="class", rsp_subtree_include=["faults", "no-scoped"])
    assert state["last_params"]["rsp-subtree-include"] == "faults,no-scoped"


def test_get_passes_unknown_parameters_through(client, state) -> None:
    # The escape hatch: a parameter this dictionary has never heard of.
    client.get("fvTenant", kind="class", rsp_subtree="full", params={"rsp-foo": "bar"})
    assert state["last_params"] == {"rsp-subtree": "full", "rsp-foo": "bar"}


def test_get_builds_a_class_or_an_mo_path(client, state) -> None:
    client.get("uni/tn-common", kind="mo")
    assert state["mo_requests"] == {"/api/mo/uni/tn-common.json": 1}


def test_get_reaches_a_fabric_node_with_the_same_token(client, state) -> None:
    data = client.get("l1PhysIf", kind="class", node="leaf101.example.com")
    assert data["imdata"][0]["l1PhysIf"]["attributes"]["id"] == "eth1/1"
    assert state["node_requests"] == [("leaf101.example.com", "/api/class/l1PhysIf.json")]
    assert "tok1" in state["node_cookie"]


def test_get_reports_an_apic_error(client) -> None:
    with pytest.raises(ApicError) as exc:
        client.get("boom", kind="class")
    assert exc.value.code == "122"


def test_get_before_login_does_not_reach_the_apic(state) -> None:
    client = Client(transport=DirectTransport(make_session(state, Clock())))
    with pytest.raises(NotLoggedInError):
        client.get("fvTenant", kind="class")


# -- list_children ----------------------------------------------------------


def test_list_children_asks_for_the_children_by_name_only(client, state) -> None:
    # The least the APIC can send while still naming each child.
    client.list_children("uni")
    assert state["last_path"] == "/api/mo/uni.json"
    assert state["last_params"] == {"query-target": "children", "rsp-prop-include": "naming-only"}


def test_list_children_returns_the_dns_bare_and_sorted(client) -> None:
    # Bare, so that one of them is what get() takes as its target.
    assert client.list_children("uni/tn-common") == [
        "uni/tn-common/BD-default",
        "uni/tn-common/ap-web",
    ]


def test_list_children_leaves_nothing_out(client) -> None:
    """A browse shows the runtime containers a comparison walks past.

    This is the one place list_children and the diff walk differ, and it is on
    purpose: the walk narrows to config-only because it decides what gets
    compared, and an MO nobody configured has nothing to compare against.
    """

    assert "uni/epp" in client.list_children("uni")


def test_list_children_reaches_a_fabric_node_with_the_same_token(client, state) -> None:
    # The switch's own MIT, whose root is sys rather than uni.
    assert client.list_children("sys", node="leaf101.example.com") == [
        "sys/phys-[eth1/1]",
        "sys/phys-[eth1/2]",
    ]
    assert state["node_requests"] == [("leaf101.example.com", "/api/mo/sys.json")]


# -- post ------------------------------------------------------------------


def test_post_sends_text_exactly_as_given(client, state) -> None:
    body = '{"fvTenant": {"attributes": {"name": "demo"}}}'
    client.post("uni/tn-demo", body, kind="mo")
    assert state["last_method"] == "POST"
    assert state["last_body"] == body


def test_post_serializes_an_object(client, state) -> None:
    client.post("uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo")
    assert json.loads(state["last_body"]) == {"fvTenant": {"attributes": {"name": "demo"}}}


@pytest.mark.parametrize(("body", "message"), [("", "empty body"), ("{no", "invalid JSON")])
def test_post_rejects_a_body_it_cannot_read(client, state, body, message) -> None:
    with pytest.raises(ValueError) as exc:
        client.post("uni/tn-demo", body, kind="mo")
    assert message in str(exc.value)
    assert "last_method" not in state


# -- dry_run (what a POST would change) ------------------------------------


def test_dry_run_fetches_the_current_state_and_never_posts(client, state) -> None:
    changes = client.dry_run(
        "uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo"
    )
    assert state["last_method"] == "GET"
    # The whole subtree, and only the settable properties.
    assert state["last_params"] == {"rsp-subtree": "full", "rsp-prop-include": "config-only"}
    assert [(c.kind, c.dn) for c in changes] == [("modified", "uni/tn-common")]
    assert changes[0].attributes == {"name": ("common", "demo")}


def test_dry_run_returns_nothing_when_the_body_would_change_nothing(client) -> None:
    assert (
        client.dry_run("uni/tn-demo", {"fvTenant": {"attributes": {"name": "common"}}}, kind="mo")
        == []
    )


def test_dry_run_reports_a_new_child(client) -> None:
    changes = client.dry_run(
        "uni/tn-demo",
        {
            "fvTenant": {
                "attributes": {"name": "common"},
                "children": [{"fvBD": {"attributes": {"name": "bd1"}}}],
            }
        },
        kind="mo",
    )
    assert [c.kind for c in changes] == ["created"]
    assert changes[0].class_name == "fvBD"


def test_dry_run_walks_every_root_of_an_array_body(client, state) -> None:
    changes = client.dry_run(
        "uni",
        [
            {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "a"}}},
            {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "b"}}},
        ],
        kind="mo",
    )
    assert state["mo_requests"] == {"/api/mo/uni/tn-demo.json": 2}
    assert [c.kind for c in changes] == ["modified", "modified"]


def test_dry_run_splits_a_wrapped_body_into_one_request_per_top_level_mo(client, state) -> None:
    # uni fetched whole with rsp-subtree=full is the request a large fabric
    # times out on, and a merged body always names uni. The wrapper carries no
    # configuration of its own, so each MO under it is fetched on its own.
    client.dry_run(
        "uni",
        {
            "polUni": {
                "attributes": {"dn": "uni"},
                "children": [
                    {"fvTenant": {"attributes": {"rn": "tn-common", "descr": "x"}}},
                    {"fvTenant": {"attributes": {"rn": "tn-infra"}}},
                ],
            }
        },
        kind="mo",
    )
    assert state["mo_requests"] == {
        "/api/mo/uni/tn-common.json": 1,
        "/api/mo/uni/tn-infra.json": 1,
    }


def test_dry_run_compares_a_wrapped_child_against_its_own_subtree(client) -> None:
    changes = client.dry_run(
        "uni",
        {
            "polUni": {
                "attributes": {"dn": "uni"},
                "children": [{"fvTenant": {"attributes": {"rn": "tn-infra", "descr": "x"}}}],
            }
        },
        kind="mo",
    )
    assert [(c.kind, c.dn) for c in changes] == [("modified", "uni/tn-infra")]
    assert changes[0].attributes == {"descr": (None, "x")}


def test_dry_run_fetches_nothing_for_a_child_it_cannot_name(client, state) -> None:
    # No name, so no RN: the DN is a stand-in and no MO on the fabric answers
    # to it. Fetching it would only ask the APIC about an MO that cannot exist.
    changes = client.dry_run(
        "uni",
        {
            "polUni": {
                "attributes": {"dn": "uni"},
                "children": [{"fvTenant": {"attributes": {"descr": "x"}}}],
            }
        },
        kind="mo",
    )
    assert state["mo_requests"] == {}
    assert [c.kind for c in changes] == ["warning", "created"]


def test_dry_run_refuses_a_paged_answer_rather_than_reporting_it_as_a_change(monkeypatch) -> None:
    # The APIC says the tenant holds three MOs and hands back two. Comparing
    # the two would report everything past the page as an MO this POST creates.
    def get(target, kind, params, node, *, autostart=True):
        return {
            "totalCount": "3",
            "imdata": [
                {"fvTenant": {"attributes": {"dn": "uni/tn-demo"}}},
                {"fvBD": {"attributes": {"dn": "uni/tn-demo/BD-a"}}},
            ],
        }

    monkeypatch.setattr(ipc, "get", get)
    with pytest.raises(ApicError) as exc:
        Client(transport=DaemonTransport()).dry_run(
            "uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo"
        )
    assert "uni/tn-demo: the APIC returned 2 of 3 MOs" in str(exc.value)


def test_dry_run_needs_a_dn_it_can_work_out(client, state) -> None:
    with pytest.raises(ValueError) as exc:
        client.dry_run("fvTenant", {"fvTenant": {"attributes": {"name": "x"}}}, kind="class")
    assert "cannot determine the target DN" in str(exc.value)
    assert state["mo_requests"] == {}


def test_dry_run_refuses_a_body_not_written_as_aci_expects(client, state) -> None:
    # Refused before any GET goes out on the strength of it, and refused as a
    # malformed body rather than as a DN that cannot be worked out.
    with pytest.raises(ValueError) as exc:
        client.dry_run("uni/tn-demo", {"totalCount": "1", "imdata": []}, kind="mo")
    assert "GET response" in str(exc.value)
    assert state["mo_requests"] == {}


def test_a_raw_post_sends_a_malformed_body_untouched(client, state) -> None:
    # The one input path that is not checked: a raw POST never parses the body,
    # and what the APIC makes of this one is the APIC's answer to give.
    client.post("uni/tn-demo", '{"fvTenant": null}', kind="mo")
    assert state["last_method"] == "POST"


def test_post_with_dry_run_compares_instead_of_sending(client, state) -> None:
    body = {"fvTenant": {"attributes": {"name": "demo"}}}
    changes = client.post("uni/tn-demo", body, kind="mo", dry_run=True)
    assert isinstance(changes[0], mo_.Change)
    assert state["last_method"] == "GET"


# -- diff (the fabric against an intended configuration) --------------------


# A configuration describing one MO of the mock fabric exactly. A diff takes one
# body and refuses an empty one, so a test that is about something else still
# has to say what the fabric is meant to be carrying.
INFRA = {"fvTenant": {"attributes": {"dn": "uni/tn-infra", "name": "infra"}}}
# One naming an MO inside tn-common, for the tests that exclude that tenant: the
# configuration is not empty, but nothing of it survives the exclusion.
IN_COMMON = {"fvBD": {"attributes": {"dn": "uni/tn-common/BD-default"}}}


def test_diff_fetches_one_subtree_per_top_level_mo_and_never_writes(client, state) -> None:
    client.diff(INFRA)
    assert state["last_method"] == "GET"
    # uni is listed once, then each of its children is fetched whole.
    assert state["mo_requests"] == {
        "/api/mo/uni.json": 1,
        "/api/mo/uni/tn-common.json": 1,
        "/api/mo/uni/tn-infra.json": 1,
    }
    assert state["last_params"] == {"rsp-subtree": "full", "rsp-prop-include": "config-only"}


def test_diff_reports_everything_the_configuration_leaves_out(client) -> None:
    changes = client.diff(INFRA)
    assert [(c.kind, c.dn) for c in changes] == [
        ("extra", "uni/tn-common/BD-default"),
        ("extra", "uni/tn-common/ap-web"),
    ]


def test_diff_is_empty_when_the_configuration_describes_the_fabric(client) -> None:
    # One body, as post takes one body: a list of MOs is one body. Several are
    # folded into one beforehand by a4i.merge.
    changes = client.diff(
        [
            {"fvAp": {"attributes": {"dn": "uni/tn-common/ap-web"}}},
            {"fvBD": {"attributes": {"dn": "uni/tn-common/BD-default"}}},
            INFRA,
        ]
    )
    assert changes == []


def test_diff_takes_the_body_as_json_text_as_post_does(client) -> None:
    assert client.diff(json.dumps(INFRA)) == client.diff(INFRA)


def test_diff_refuses_a_configuration_that_describes_no_mo(client) -> None:
    # Taken at face value it means every MO on the fabric is extra, which is
    # never what an empty input meant.
    with pytest.raises(ValueError) as exc:
        client.diff([])
    assert "empty" in str(exc.value)


def test_diff_leaves_an_excluded_subtree_out_of_the_report(client) -> None:
    # The configuration says nothing but what the exclusion then removes, which
    # is a comparison narrowed to nothing rather than an input that said nothing.
    changes = client.diff(IN_COMMON, exclude="uni/tn-common")
    # Everything under tn-common goes with it; tn-infra is untouched.
    assert [(c.kind, c.dn) for c in changes] == [("extra", "uni/tn-infra")]


def test_diff_fetches_an_excluded_subtree_all_the_same(client, state) -> None:
    # The exclusion narrows what is compared, not what is read: the fabric sees
    # the same requests either way.
    client.diff(INFRA, exclude="uni/tn-common")
    assert state["mo_requests"] == {
        "/api/mo/uni.json": 1,
        "/api/mo/uni/tn-common.json": 1,
        "/api/mo/uni/tn-infra.json": 1,
    }


def test_diff_takes_a_sequence_of_excluded_dns(client) -> None:
    assert client.diff(INFRA, exclude=["uni/tn-common", "uni/tn-infra"]) == []


def test_diff_names_the_subtree_it_could_not_fetch(client, state) -> None:
    state["fail_path"] = "/api/mo/uni/tn-infra.json"
    with pytest.raises(ApicError) as exc:
        client.diff(INFRA)
    assert "uni/tn-infra" in str(exc.value)
    assert "forbidden" in str(exc.value)


# -- the session a client owns ---------------------------------------------


def test_a_client_needs_a_host_or_a_transport() -> None:
    with pytest.raises(TypeError):
        Client()


def test_a_client_built_from_a_host_owns_a_session() -> None:
    # __init__ is what tests/test_awaited.py exempts, so each side is on its
    # own here: this is the branch that builds a session rather than taking one.
    client = Client("apic1.example.com", verify=False)
    assert client._session is not None
    assert client._session.base_url == "https://apic1.example.com"
    client.close()


def test_a_client_built_from_a_host_hands_the_timeout_to_its_session() -> None:
    client = Client("apic1.example.com", verify=False, timeout=120.0)
    assert client._session is not None
    assert client._session.timeout == 120.0
    client.close()


def test_a_client_asking_for_no_timeout_gets_the_default_one() -> None:
    # None here is "whichever the session says", not "wait forever": the default
    # is read from the session so that it is written down once.
    client = Client("apic1.example.com", verify=False)
    assert client._session is not None
    assert client._session.timeout == DEFAULT_TIMEOUT
    client.close()


def test_a_client_without_a_session_of_its_own_cannot_log_in() -> None:
    client = Client(transport=DaemonTransport())
    with pytest.raises(TypeError):
        client.login("admin", "pw")
    # Closing is still safe: there is nothing to close.
    client.close()


def test_a_transport_of_ones_own_settles_the_timeout_itself(state) -> None:
    """As verify: what describes a session this client did not build is not its say."""

    session = make_session(state, Clock())
    client = Client(timeout=120.0, transport=DirectTransport(session))
    assert client._session is session
    assert session.timeout == DEFAULT_TIMEOUT
    client.close()


def test_the_context_manager_closes_the_session(state) -> None:
    with Client(transport=DirectTransport(make_session(state, Clock()))) as client:
        client.login("admin", "pw")
        assert client.logged_in
    with pytest.raises(RuntimeError):
        client.get("fvTenant", kind="class")


def test_the_token_is_refreshed_past_its_half_life(state) -> None:
    clock = Clock()
    client = Client(transport=DirectTransport(make_session(state, clock)))
    client.login("admin", "pw")
    clock.advance(301)
    client.get("fvTenant", kind="class")
    assert state["token_n"] == 2
    assert "tok2" in state["last_cookie"]


def test_an_expired_token_asks_for_a_new_login(state) -> None:
    clock = Clock()
    client = Client(transport=DirectTransport(make_session(state, clock)))
    client.login("admin", "pw")
    clock.advance(601)
    with pytest.raises(SessionExpiredError):
        client.get("fvTenant", kind="class")


# -- the daemon transport --------------------------------------------------
#
# The daemon flattens an exception into a wire error, so the transport has to
# restore it: a caller catches the same exception on either side of the socket.


# What a daemon error turns back into is a4i.errors' -- see tests/test_errors.py.


def test_the_daemon_transport_sends_what_the_daemon_expects(monkeypatch) -> None:
    sent: list[dict] = []

    def get(target, kind, params, node, *, autostart=True):
        sent.append({"op": "get", "target": target, "kind": kind, "params": params, "node": node})
        return {"imdata": []}

    def post(target, kind, body, *, autostart=True):
        sent.append({"op": "post", "target": target, "kind": kind, "body": body})
        return {"imdata": []}

    monkeypatch.setattr(ipc, "get", get)
    monkeypatch.setattr(ipc, "post", post)
    client = Client(transport=DaemonTransport())
    client.get("fvTenant", kind="class", rsp_subtree="full", node="leaf101.example.com")
    client.post("uni/tn-demo", '{"fvTenant":{}}', kind="mo")
    # The kind travels with the target rather than being encoded into it, so the
    # daemon builds the path from what the caller meant.
    assert sent == [
        {
            "op": "get",
            "target": "fvTenant",
            "kind": "class",
            "params": {"rsp-subtree": "full"},
            "node": "leaf101.example.com",
        },
        {"op": "post", "target": "uni/tn-demo", "kind": "mo", "body": '{"fvTenant":{}}'},
    ]


# -- the package's public names --------------------------------------------


def test_the_public_names_resolve_without_importing_httpx2_up_front() -> None:
    import a4i

    assert a4i.Client is Client
    assert a4i.Change is mo_.Change
    assert issubclass(a4i.ApicError, a4i.A4iError)
    with pytest.raises(AttributeError):
        getattr(a4i, "nonexistent")  # noqa: B009 - the point is the lookup failing


def test_the_apic_host_is_normalized(state) -> None:
    client = Client(transport=DirectTransport(make_session(state, Clock())))
    assert client._session is not None
    assert client._session.base_url == f"https://{APIC_HOST}"


def test_diff_refuses_a_paged_answer_rather_than_reporting_it_as_a_diff(monkeypatch) -> None:
    # The APIC says uni has three children and hands back two. Comparing the
    # two would report everything under the third as missing from a fabric that
    # is carrying it, so the fetch fails instead.
    def get(target, kind, params, node, *, autostart=True):
        return {
            "totalCount": "3",
            "imdata": [
                {"fvTenant": {"attributes": {"dn": "uni/tn-common"}}},
                {"fvTenant": {"attributes": {"dn": "uni/tn-infra"}}},
            ],
        }

    monkeypatch.setattr(ipc, "get", get)
    with pytest.raises(ApicError) as exc:
        Client(transport=DaemonTransport()).diff(INFRA)
    assert "uni: the APIC returned 2 of 3 MOs" in str(exc.value)


def test_diff_does_not_walk_the_runtime_containers_under_uni(client, state) -> None:
    # uni holds runtime children as well as configuration. Listing them with
    # "config-only", as the subtrees are then fetched, keeps them out of the
    # walk: an intended configuration never names one, so every one walked
    # would be reported extra.
    client.diff(INFRA)
    assert "/api/mo/uni/epp.json" not in state["mo_requests"]
    assert [c.dn for c in client.diff(INFRA)] == [
        "uni/tn-common/BD-default",
        "uni/tn-common/ap-web",
    ]
