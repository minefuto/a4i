from __future__ import annotations

import json
import ssl
import warnings

import httpx2
import pytest

from a4i.session import (
    NODE_CLIENT_MAX,
    ApicError,
    NotLoggedInError,
    SessionExpiredError,
    _default_client,
    _ssl_context,
)
from apic_mock import Clock, make_session


def test_login_stores_token_in_memory() -> None:
    state: dict = {}
    session = make_session(state, Clock())
    session.login("admin", "pw")
    assert session.logged_in
    assert session.user == "admin"
    assert session.refresh_timeout == 600
    # Token is only in memory, exposed via the client cookie, never a file.
    assert session._token == "tok1"


def test_login_failure_raises_with_apic_text() -> None:
    state: dict = {"fail_login": True}
    session = make_session(state, Clock())
    with pytest.raises(ApicError) as exc:
        session.login("admin", "bad")
    assert "Invalid credentials" in str(exc.value)
    assert not session.logged_in


def test_get_after_login() -> None:
    session = make_session({}, Clock())
    session.login("admin", "pw")
    data = session.get("/api/class/fvTenant.json")
    assert data["imdata"][0]["fvTenant"]["attributes"]["name"] == "common"


def test_request_requires_login() -> None:
    session = make_session({}, Clock())
    with pytest.raises(NotLoggedInError):
        session.get("/api/class/fvTenant.json")


def test_error_response_raises() -> None:
    session = make_session({}, Clock())
    session.login("admin", "pw")
    with pytest.raises(ApicError) as exc:
        session.get("/api/class/boom.json")
    assert "boom failed" in str(exc.value)


def test_refresh_updates_token() -> None:
    state: dict = {}
    clock = Clock()
    session = make_session(state, clock)
    session.login("admin", "pw")  # tok1
    clock.advance(301)  # past half of 600 -> lazy refresh on next request
    session.get("/api/class/fvTenant.json")
    assert state["last_cookie"] == "APIC-cookie=tok2"


def test_no_refresh_before_half_life() -> None:
    state: dict = {}
    clock = Clock()
    session = make_session(state, clock)
    session.login("admin", "pw")  # tok1
    clock.advance(299)  # before half of 600
    session.get("/api/class/fvTenant.json")
    assert state["last_cookie"] == "APIC-cookie=tok1"


def test_expired_session_requires_relogin() -> None:
    clock = Clock()
    session = make_session({}, clock)
    session.login("admin", "pw")
    clock.advance(600)  # full lifetime elapsed with no activity
    with pytest.raises(SessionExpiredError):
        session.get("/api/class/fvTenant.json")
    assert not session.logged_in


def test_logout_tells_the_apic() -> None:
    state: dict = {}
    session = make_session(state, Clock())
    session.login("admin", "pw")
    session.logout()
    assert len(state["logouts"]) == 1
    cookie, body = state["logouts"][0]
    assert cookie == "APIC-cookie=tok1"
    assert json.loads(body) == {"aaaUser": {"attributes": {"name": "admin"}}}
    assert not session.logged_in


def test_logout_without_a_token_sends_nothing() -> None:
    state: dict = {}
    session = make_session(state, Clock())
    session.logout()
    assert state["logouts"] == []


def test_expired_session_is_not_logged_out_on_the_apic() -> None:
    """An expired token has nothing left to end, so aaaLogout is not sent for one."""

    state: dict = {}
    clock = Clock()
    session = make_session(state, clock)
    session.login("admin", "pw")
    clock.advance(600)  # full lifetime elapsed with no activity
    session.logout()
    assert state["logouts"] == []
    assert not session.logged_in


def test_expiry_on_a_request_sends_no_logout() -> None:
    state: dict = {}
    clock = Clock()
    session = make_session(state, clock)
    session.login("admin", "pw")
    clock.advance(600)
    with pytest.raises(SessionExpiredError):
        session.get("/api/class/fvTenant.json")
    assert state["logouts"] == []


def test_logout_drops_the_token_even_when_the_apic_refuses() -> None:
    state: dict = {"fail_logout": True}
    session = make_session(state, Clock())
    session.login("admin", "pw")
    session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    with pytest.raises(ApicError) as exc:
        session.logout()
    assert "logout refused" in str(exc.value)
    assert not session.logged_in
    assert session._node_clients == {}


def test_logout_drops_the_token_when_the_apic_is_unreachable() -> None:
    state: dict = {}
    session = make_session(state, Clock())
    session.login("admin", "pw")
    state["unreachable"] = {"apic.test"}
    with pytest.raises(ApicError) as exc:
        session.logout()
    assert "cannot reach https://apic.test" in str(exc.value)
    assert not session.logged_in


def test_unreachable_host_names_the_destination() -> None:
    state: dict = {"unreachable": {"apic.test"}}
    session = make_session(state, Clock())
    with pytest.raises(ApicError) as exc:
        session.login("admin", "pw")
    assert "cannot reach https://apic.test" in str(exc.value)


# -- querying a fabric node with the APIC's token --------------------------


def test_node_get_uses_the_apic_token() -> None:
    state: dict = {}
    session = make_session(state, Clock())
    session.login("admin", "pw")
    data = session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert data["imdata"][0]["l1PhysIf"]["attributes"]["id"] == "eth1/1"
    assert state["node_requests"] == [("leaf101.test", "/api/class/l1PhysIf.json")]
    assert state["node_cookie"] == "APIC-cookie=tok1"
    # The APIC is untouched by a node query beyond the login itself.
    assert state["token_n"] == 1


def test_node_client_is_reused_per_host() -> None:
    session = make_session({}, Clock())
    session.login("admin", "pw")
    session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    first = session._node_clients["https://leaf101.test"]
    session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert session._node_clients["https://leaf101.test"] is first
    session.get("/api/class/l1PhysIf.json", host="leaf102.test")
    assert list(session._node_clients) == ["https://leaf101.test", "https://leaf102.test"]


def test_node_clients_inherit_the_login_tls_setting() -> None:
    state: dict = {}
    session = make_session(state, Clock(), verify=False)
    session.login("admin", "pw")
    session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert state["verify"]["https://leaf101.test"] is False


# -- TLS verification ------------------------------------------------------
#
# The real client factory, which the mocked sessions above stand in for. What
# --ca gives is a path, and httpx2 takes one only by deprecation, so it is an
# SSL context that has to reach the client.


@pytest.mark.parametrize("verify", [True, False])
def test_a_bool_verify_reaches_the_client_as_it_is(verify: bool) -> None:
    assert _ssl_context(verify) is verify


def test_a_ca_directory_is_loaded_through_capath(tmp_path) -> None:
    # An OpenSSL CA path is a directory of hashed symlinks rather than one
    # bundle, and is read lazily, so an empty one is still a valid one.
    context = _ssl_context(str(tmp_path))
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED


def test_a_ca_bundle_that_is_not_there_is_reported_at_once(tmp_path) -> None:
    # The file branch reads the bundle there and then, unlike capath: a mistyped
    # --ca fails the login rather than quietly verifying against nothing.
    with pytest.raises(FileNotFoundError):
        _ssl_context(str(tmp_path / "missing.pem"))


def test_a_ca_path_does_not_reach_httpx2_as_a_deprecated_string(tmp_path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        client = _default_client("https://apic.test", str(tmp_path))
    client.close()


def test_node_get_refreshes_against_the_apic() -> None:
    state: dict = {}
    clock = Clock()
    session = make_session(state, clock)
    session.login("admin", "pw")  # tok1
    clock.advance(301)  # past half of 600
    session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    # The refresh went to the APIC, and the node saw the token it produced.
    assert state["token_n"] == 2
    assert state["node_cookie"] == "APIC-cookie=tok2"


def test_node_get_on_expired_session_requires_relogin() -> None:
    state: dict = {}
    clock = Clock()
    session = make_session(state, clock)
    session.login("admin", "pw")
    clock.advance(600)
    with pytest.raises(SessionExpiredError):
        session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert state["node_requests"] == []


def test_node_clients_are_evicted_and_closed() -> None:
    session = make_session({}, Clock())
    session.login("admin", "pw")
    for n in range(NODE_CLIENT_MAX + 1):
        session.get("/api/class/l1PhysIf.json", host=f"leaf{n}.test")
    assert len(session._node_clients) == NODE_CLIENT_MAX
    assert "https://leaf0.test" not in session._node_clients


def test_logout_drops_node_clients() -> None:
    session = make_session({}, Clock())
    session.login("admin", "pw")
    session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    session.logout()
    assert session._node_clients == {}


# -- per-call timeout ------------------------------------------------------


def test_get_uses_the_client_timeout_by_default() -> None:
    state: dict = {}
    session = make_session(state, Clock())
    session.login("admin", "pw")
    session.get("/api/class/fvTenant.json")
    # Asking for no timeout means the client's own, not the "wait forever" that
    # httpx2 reads a literal None as.
    assert state["timeouts"][-1] == httpx2.Client().timeout.read


def test_get_timeout_applies_to_the_call() -> None:
    state: dict = {}
    session = make_session(state, Clock())
    session.login("admin", "pw")
    session.get("/api/class/fvTenant.json", timeout=3.0)
    assert state["timeouts"][-1] == 3.0
    session.get("/api/class/l1PhysIf.json", host="leaf101.test", timeout=3.0)
    assert state["timeouts"][-1] == 3.0


def test_get_timeout_bounds_the_refresh_too() -> None:
    """A caller who cannot wait must not be made to wait on aaaRefresh either."""

    state: dict = {}
    clock = Clock()
    session = make_session(state, clock)
    session.login("admin", "pw")
    clock.advance(301)  # past half of 600: the next get refreshes first
    session.get("/api/class/l1PhysIf.json", host="leaf101.test", timeout=3.0)
    assert state["token_n"] == 2
    # The refresh and the node query, both bounded.
    assert state["timeouts"][-2:] == [3.0, 3.0]


def test_unreachable_node_names_the_destination() -> None:
    state: dict = {"unreachable": {"leaf101.test"}}
    session = make_session(state, Clock())
    session.login("admin", "pw")
    with pytest.raises(ApicError) as exc:
        session.get("/api/class/l1PhysIf.json", host="leaf101.test")
    assert "cannot reach https://leaf101.test" in str(exc.value)
