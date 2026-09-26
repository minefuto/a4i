"""The library entry point: the same requests the CLI makes, without a daemon."""

from __future__ import annotations

import json

import pytest

from a4i import _diff as diff
from a4i import _ipc as ipc
from a4i import _mo as mo_
from a4i import _plan as plan_
from a4i._client import Client
from a4i._errors import ApicError, NotLoggedInError, SessionExpiredError
from a4i._session import DEFAULT_TIMEOUT
from a4i._transport import DaemonTransport, DirectTransport
from apic_mock import APIC_HOST, Clock, make_session


@pytest.fixture
def state() -> dict:
    return {}


@pytest.fixture
def client(state) -> Client:
    """A logged-in client talking to the mocked APIC."""

    client = Client(_transport=DirectTransport(make_session(state, Clock())))
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
    # The keyword names are the CLI option names with underscores, and the URL
    # order is the CLI's.
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
    client = Client(_transport=DirectTransport(make_session(state, Clock())))
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

    The one place list_children and the diff walk differ on purpose: the walk
    narrows to config-only because it decides what gets compared.
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
    assert [(c.kind, c.dn) for c in changes] == [("modified", "uni/tn-demo")]
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


def test_dry_run_folds_two_roots_naming_one_mo_into_one_comparison(client, state) -> None:
    """Both sides are read into an index keyed by DN, so one MO is one entry.

    Two roots at the same DN are one MO the POST would write once. Fetching it
    twice and reporting it twice would be the body's shape leaking into the report.
    """

    changes = client.dry_run(
        "uni",
        [
            {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "a"}}},
            {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "b"}}},
        ],
        kind="mo",
    )
    assert state["mo_requests"] == {"/api/mo/uni/tn-demo.json": 1}
    assert [(c.kind, c.attributes) for c in changes] == [("modified", {"name": ("common", "b")})]


def test_dry_run_splits_a_wrapped_body_into_one_request_per_top_level_mo(client, state) -> None:
    # uni fetched whole with rsp-subtree=full is the request a large fabric times
    # out on, and the wrapper carries no configuration of its own, so each MO under
    # it is fetched on its own.
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


def test_dry_run_refuses_a_child_it_cannot_name_before_asking_the_apic(client, state) -> None:
    """No name, so no RN: the body names no one MO, and merge.read refuses it.

    The MO the body meant may well be on the fabric, so reporting a made-up DN as
    created would be the report talking about an MO nobody named. Nothing is asked
    of the APIC on the strength of it either.
    """

    with pytest.raises(ValueError) as exc:
        client.dry_run(
            "uni",
            {
                "polUni": {
                    "attributes": {"dn": "uni"},
                    "children": [{"fvTenant": {"attributes": {"descr": "x"}}}],
                }
            },
            kind="mo",
        )
    assert "fvTenant" in str(exc.value)
    assert state["mo_requests"] == {}


def test_dry_run_refuses_a_paged_answer_rather_than_reporting_it_as_a_change(monkeypatch) -> None:
    # The APIC says the tenant holds three MOs and hands back two: comparing
    # against that would report everything past the page as an MO this POST
    # creates.
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
        Client(_transport=DaemonTransport()).dry_run(
            "uni/tn-demo", {"fvTenant": {"attributes": {"name": "demo"}}}, kind="mo"
        )
    assert "uni/tn-demo: the APIC returned 2 of 3 MOs" in str(exc.value)


def test_dry_run_needs_a_dn_it_can_work_out(client, state) -> None:
    with pytest.raises(ValueError) as exc:
        client.dry_run("fvTenant", {"fvTenant": {"attributes": {"name": "x"}}}, kind="class")
    assert "cannot determine the target DN" in str(exc.value)
    assert state["mo_requests"] == {}


def test_dry_run_refuses_a_body_not_written_as_aci_expects(client, state) -> None:
    # Before any GET goes out on the strength of it, and as a malformed body rather
    # than as a DN that cannot be worked out.
    with pytest.raises(ValueError) as exc:
        client.dry_run("uni/tn-demo", {"totalCount": "1", "imdata": []}, kind="mo")
    assert "GET response" in str(exc.value)
    assert state["mo_requests"] == {}


def test_a_raw_post_sends_a_malformed_body_untouched(client, state) -> None:
    # The one input path that is not checked: a raw POST never parses the body.
    client.post("uni/tn-demo", '{"fvTenant": null}', kind="mo")
    assert state["last_method"] == "POST"


# -- plan (what a POST would change, as a body) -----------------------------

# plan and diff perform no I/O: they take the fabric a fetch read. What is tested
# here is the pair as a caller runs them, a real fetch of the mocked APIC on one
# side. What each reports on a fabric written by hand is tests/test_plan.py's and
# tests/test_diff.py's.


def test_plan_carries_the_changes_and_the_mos_they_hang_under(client) -> None:
    narrowed = plan_.create(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-common", "name": "common"},
                "children": [{"fvBD": {"attributes": {"name": "default", "mtu": "9000"}}}],
            }
        },
        fabric=client.fetch(),
    )
    tenant = narrowed["polUni"]["children"][0]["fvTenant"]
    # The tenant is only what the BD hangs under, so it carries rn and
    # status="modified" and no attribute of its own.
    assert tenant["attributes"] == {"rn": "tn-common", "status": "modified"}
    assert tenant["children"][0]["fvBD"]["attributes"]["status"] == "modified"
    assert tenant["children"][0]["fvBD"]["attributes"]["mtu"] == "9000"


def test_plan_asks_the_fabric_for_nothing(client, state) -> None:
    """Every request a plan rests on was the fetch's, and is already paid for."""

    fabric = client.fetch()
    state["mo_requests"].clear()
    plan_.create(
        {"fvTenant": {"attributes": {"dn": "uni/tn-infra", "name": "infra"}}}, fabric=fabric
    )
    assert state["mo_requests"] == {}


def test_plan_refuses_an_mo_that_cannot_be_folded_into_one_body(client) -> None:
    # merge's refusal, and its wording: a plan is posted at uni as a merged body
    # is.
    with pytest.raises(ValueError) as exc:
        plan_.create(
            {"fabricNode": {"attributes": {"dn": "topology/pod-1/node-101"}}},
            fabric=client.fetch(),
        )
    assert "into one body" in str(exc.value)


def test_plan_refuses_a_body_the_dry_run_warned_about(client) -> None:
    with pytest.raises(ValueError) as exc:
        plan_.create(
            {
                "fvTenant": {
                    "attributes": {"dn": "uni/tn-infra", "name": "infra", "status": "created"}
                }
            },
            fabric=client.fetch(),
        )
    assert "refusing to write a plan" in str(exc.value)


# -- fetch (the fabric as an intended configuration) ------------------------


def test_fetch_reads_one_subtree_per_top_level_mo_and_never_writes(client, state) -> None:
    client.fetch()
    # uni is listed once, then each of its children is fetched whole.
    assert state["mo_requests"] == {
        "/api/mo/uni.json": 1,
        "/api/mo/uni/tn-common.json": 1,
        "/api/mo/uni/tn-infra.json": 1,
    }
    assert state["last_method"] == "GET"
    assert state["last_params"] == {"rsp-subtree": "full", "rsp-prop-include": "config-only"}


def test_fetch_returns_a_body_shaped_as_merge_shapes_one(client) -> None:
    body = client.fetch()
    assert body["polUni"]["attributes"] == {"dn": "uni"}
    tenants = [next(iter(child)) for child in body["polUni"]["children"]]
    assert tenants == ["fvTenant", "fvTenant"]
    common = body["polUni"]["children"][0]["fvTenant"]
    # Nested under the MO it hangs off, named by its rn, with no absolute dn.
    assert common["attributes"]["rn"] == "tn-common"
    assert "dn" not in common["attributes"]
    # RN order, as merge writes siblings: "BD-default" before "ap-web".
    assert [next(iter(child)) for child in common["children"]] == ["fvBD", "fvAp"]


def test_fetch_then_diff_finds_no_difference(client) -> None:
    """The round trip: what fetch writes is what diff compares against.

    If merge, diff and this ever drift apart on what a body means, the fabric
    starts differing from itself.
    """

    fabric = client.fetch()
    assert diff.compare(fabric, fabric=fabric) == []


# -- diff (the fabric against an intended configuration) --------------------


# A configuration describing one MO of the mock fabric exactly. A diff refuses an
# empty body, so a test about something else still has to say what the fabric is
# meant to be carrying.
INFRA = {"fvTenant": {"attributes": {"dn": "uni/tn-infra", "name": "infra"}}}
# One naming an MO inside tn-common, for the tests that exclude that tenant: not
# empty, but nothing of it survives the exclusion.
IN_COMMON = {
    "fvTenant": {
        "attributes": {"dn": "uni/tn-common", "name": "common"},
        "children": [{"fvBD": {"attributes": {"name": "default"}}}],
    }
}


def test_diff_reports_everything_the_configuration_leaves_out(client) -> None:
    changes = diff.compare(INFRA, fabric=client.fetch())
    # A wholly extra subtree is its top MO alone, the MOs below it counted.
    assert [(c.kind, c.dn) for c in changes] == [("extra", "uni/tn-common")]
    assert changes[0].child_count == 2


def test_diff_is_empty_when_the_configuration_describes_the_fabric(client) -> None:
    # One body, as post takes one body: a list of MOs is one body.
    changes = diff.compare(
        [
            {"fvTenant": {"attributes": {"dn": "uni/tn-common", "name": "common"}}},
            {"fvAp": {"attributes": {"dn": "uni/tn-common/ap-web"}}},
            {"fvBD": {"attributes": {"dn": "uni/tn-common/BD-default"}}},
            INFRA,
        ],
        fabric=client.fetch(),
    )
    assert changes == []


def test_diff_takes_the_body_as_json_text_as_post_does(client) -> None:
    fabric = client.fetch()
    assert diff.compare(json.dumps(INFRA), fabric=fabric) == diff.compare(INFRA, fabric=fabric)


def test_diff_refuses_a_configuration_that_describes_no_mo(client) -> None:
    # Taken at face value it means every MO on the fabric is extra.
    with pytest.raises(ValueError) as exc:
        diff.compare([], fabric=client.fetch())
    assert "empty" in str(exc.value)


def test_diff_leaves_an_excluded_subtree_out_of_the_report(client) -> None:
    # A comparison narrowed to nothing, rather than an input that said nothing.
    changes = diff.compare(IN_COMMON, fabric=client.fetch(), exclude="uni/tn-common")
    # Everything under tn-common goes with it; tn-infra is untouched.
    assert [(c.kind, c.dn) for c in changes] == [("extra", "uni/tn-infra")]


def test_diff_takes_a_sequence_of_excluded_dns(client) -> None:
    assert (
        diff.compare(INFRA, fabric=client.fetch(), exclude=["uni/tn-common", "uni/tn-infra"]) == []
    )


def test_fetch_names_the_subtree_it_could_not_read(client, state) -> None:
    state["fail_path"] = "/api/mo/uni/tn-infra.json"
    with pytest.raises(ApicError) as exc:
        client.fetch()
    assert "uni/tn-infra" in str(exc.value)
    assert "forbidden" in str(exc.value)


# -- the session a client owns ---------------------------------------------


def test_a_client_needs_a_host_or_a_transport() -> None:
    with pytest.raises(TypeError):
        Client()


def test_a_client_built_from_a_host_owns_a_session() -> None:
    # __init__ is what tests/test_awaited.py exempts, so each side is checked on
    # its own here.
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
    # None here is "whichever the session says", not "wait forever".
    client = Client("apic1.example.com", verify=False)
    assert client._session is not None
    assert client._session.timeout == DEFAULT_TIMEOUT
    client.close()


def test_a_client_without_a_session_of_its_own_cannot_log_in() -> None:
    client = Client(_transport=DaemonTransport())
    with pytest.raises(TypeError):
        client.login("admin", "pw")
    # Closing is still safe: there is nothing to close.
    client.close()


def test_a_transport_of_ones_own_settles_the_timeout_itself(state) -> None:
    """As verify: what describes a session this client did not build is not its say."""

    session = make_session(state, Clock())
    client = Client(timeout=120.0, _transport=DirectTransport(session))
    assert client._session is session
    assert session.timeout == DEFAULT_TIMEOUT
    client.close()


def test_the_context_manager_closes_the_session(state) -> None:
    with Client(_transport=DirectTransport(make_session(state, Clock()))) as client:
        client.login("admin", "pw")
        assert client.logged_in
    with pytest.raises(RuntimeError):
        client.get("fvTenant", kind="class")


def test_the_token_is_refreshed_past_its_half_life(state) -> None:
    clock = Clock()
    client = Client(_transport=DirectTransport(make_session(state, clock)))
    client.login("admin", "pw")
    clock.advance(301)
    client.get("fvTenant", kind="class")
    assert state["token_n"] == 2
    assert "tok2" in state["last_cookie"]


def test_an_expired_token_asks_for_a_new_login(state) -> None:
    clock = Clock()
    client = Client(_transport=DirectTransport(make_session(state, clock)))
    client.login("admin", "pw")
    clock.advance(601)
    with pytest.raises(SessionExpiredError):
        client.get("fvTenant", kind="class")


# -- the daemon transport --------------------------------------------------
#
# The daemon flattens an exception into a wire error, so the transport has to
# restore it: a caller catches the same exception on either side of the socket.


# What a daemon error turns back into is tests/test_errors.py's.


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
    client = Client(_transport=DaemonTransport())
    client.get("fvTenant", kind="class", rsp_subtree="full", node="leaf101.example.com")
    client.post("uni/tn-demo", '{"fvTenant":{}}', kind="mo")
    # The kind travels with the target rather than being encoded into it.
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
    client = Client(_transport=DirectTransport(make_session(state, Clock())))
    assert client._session is not None
    assert client._session.base_url == f"https://{APIC_HOST}"


def test_fetch_refuses_a_paged_answer_rather_than_holding_half_a_fabric(monkeypatch) -> None:
    # The APIC says uni has three children and hands back two: the fetch fails
    # rather than report everything under the third missing.
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
        Client(_transport=DaemonTransport()).fetch()
    assert "uni: the APIC returned 2 of 3 MOs" in str(exc.value)


def test_fetch_does_not_walk_the_runtime_containers_under_uni(client, state) -> None:
    # uni holds runtime children as well as configuration, and an intended
    # configuration never names one, so every one walked would be reported extra.
    fabric = client.fetch()
    assert "/api/mo/uni/epp.json" not in state["mo_requests"]
    assert [c.dn for c in diff.compare(INFRA, fabric=fabric)] == ["uni/tn-common"]
