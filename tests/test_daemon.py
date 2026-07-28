from __future__ import annotations

import contextlib
import os
import shutil
import socket as socket_mod
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from a4i import ipc
from a4i.daemon import Daemon
from a4i.daemon import main as daemon_main
from a4i.ipc import DaemonError
from apic_mock import Clock, make_session_factory


def _wait_for_socket(path) -> None:
    for _ in range(200):
        sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        try:
            sock.connect(str(path))
            sock.close()
            return
        except OSError:
            time.sleep(0.01)
    raise RuntimeError("daemon did not start")


@pytest.fixture
def daemon(monkeypatch):
    state: dict = {}
    clock = Clock()
    # A short path under the system temp dir: AF_UNIX paths are length-limited.
    # The daemon creates the enclosing directory itself, 0700, as in production.
    sock_dir = Path(tempfile.gettempdir()) / f"a4i-t-{uuid.uuid4().hex[:8]}"
    sock_path = sock_dir / "daemon.sock"
    server = Daemon(str(sock_path), clock=clock, session_factory=make_session_factory(state, clock))
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    _wait_for_socket(sock_path)
    monkeypatch.setattr(ipc, "socket_path", lambda: sock_path)
    yield state, clock
    with contextlib.suppress(DaemonError):
        ipc.request("stop", autostart=False)
    thread.join(timeout=2)
    shutil.rmtree(sock_dir, ignore_errors=True)


def _login(user: str = "admin") -> dict:
    return ipc.request("login", host="apic.test", user=user, password="pw", verify=False)


def test_login_and_get(daemon) -> None:
    info = _login()
    assert info["user"] == "admin"
    data = ipc.request("get", target="fvTenant", kind="class", params={})
    assert data["imdata"][0]["fvTenant"]["attributes"]["name"] == "common"


def test_query_parameters_survive_the_round_trip(daemon) -> None:
    state, _ = daemon
    _login()
    params = {
        "query-target-filter": 'eq(fvTenant.descr,"a b")',
        "order-by": "fvTenant.name|desc",
    }
    ipc.request("get", target="fvTenant", kind="class", params=params)
    # Quotes, parens, commas and the pipe are what ACI filters and order-by are
    # made of, and they have to cross JSON over the socket and httpx2's URL
    # encoding without being mangled.
    assert state["last_params"] == params


def test_get_without_node_goes_to_the_apic(daemon) -> None:
    state, _ = daemon
    _login()
    ipc.request("get", target="fvTenant", kind="class", params={}, node=None)
    assert state["node_requests"] == []


def test_get_reads_the_target_as_the_kind_it_is_told(daemon) -> None:
    state, _ = daemon
    _login()
    # The same text, read two ways: nothing about "uni/tn-common" decides this.
    ipc.request("get", target="uni/tn-common", kind="mo", params={})
    assert state["last_path"] == "/api/mo/uni/tn-common.json"
    ipc.request("get", target="uni/tn-common", kind="class", params={})
    assert state["last_path"] == "/api/class/uni/tn-common.json"


def test_get_with_node_reaches_the_node(daemon) -> None:
    state, _ = daemon
    _login()
    data = ipc.request("get", target="l1PhysIf", kind="class", params={}, node="leaf101.test")
    assert data["imdata"][0]["l1PhysIf"]["attributes"]["id"] == "eth1/1"
    assert state["node_requests"] == [("leaf101.test", "/api/class/l1PhysIf.json")]
    # Same token as the APIC login, no second authentication.
    assert state["node_cookie"] == "APIC-cookie=tok1"


def test_get_requires_login(daemon) -> None:
    with pytest.raises(DaemonError) as exc:
        ipc.request("get", target="fvTenant", kind="class", params={})
    assert exc.value.type == "not_logged_in"


def test_lazy_refresh_on_command(daemon) -> None:
    state, clock = daemon
    _login()  # tok1
    clock.advance(301)  # past half-life
    ipc.request("get", target="fvTenant", kind="class", params={})
    assert state["last_cookie"] == "APIC-cookie=tok2"


def test_no_refresh_before_half_life(daemon) -> None:
    state, clock = daemon
    _login()  # tok1
    clock.advance(299)
    ipc.request("get", target="fvTenant", kind="class", params={})
    assert state["last_cookie"] == "APIC-cookie=tok1"


def test_expired_session(daemon) -> None:
    _, clock = daemon
    _login()
    clock.advance(600)  # full lifetime with no activity
    with pytest.raises(DaemonError) as exc:
        ipc.request("get", target="fvTenant", kind="class", params={})
    assert exc.value.type == "expired"


def test_status_and_logout(daemon) -> None:
    assert ipc.request("status", autostart=False)["logged_in"] is False
    _login()
    status = ipc.request("status", autostart=False)
    assert status["logged_in"] and status["user"] == "admin"
    ipc.request("logout", autostart=False)
    assert ipc.request("status", autostart=False)["logged_in"] is False


def test_logout_tells_the_apic(daemon) -> None:
    state, _ = daemon
    _login()
    reply = ipc.request("logout", autostart=False)
    assert reply == {"logged_out": True, "apic_error": None}
    assert len(state["logouts"]) == 1


def test_stop_tells_the_apic(daemon) -> None:
    state, _ = daemon
    _login()
    reply = ipc.request("stop", autostart=False)
    assert reply == {"stopping": True, "apic_error": None}
    assert len(state["logouts"]) == 1


def test_logout_reports_an_apic_that_refuses(daemon) -> None:
    state, _ = daemon
    _login()
    state["fail_logout"] = True
    reply = ipc.request("logout", autostart=False)
    assert reply["logged_out"] is True
    assert "logout refused" in reply["apic_error"]
    # The token is gone here either way, so the daemon is logged out.
    assert ipc.request("status", autostart=False)["logged_in"] is False


def test_stop_reports_an_apic_that_refuses(daemon) -> None:
    state, _ = daemon
    _login()
    state["fail_logout"] = True
    reply = ipc.request("stop", autostart=False)
    assert reply["stopping"] is True
    assert "logout refused" in reply["apic_error"]


def test_expiry_does_not_tell_the_apic(daemon) -> None:
    state, clock = daemon
    _login()
    clock.advance(600)  # full lifetime with no activity
    # The idle tick drops the expired session; nothing is sent for a dead token.
    with pytest.raises(DaemonError):
        ipc.request("get", target="fvTenant", kind="class", params={})
    assert state["logouts"] == []


# -- startup -----------------------------------------------------------------


def test_daemon_refuses_an_unsafe_socket_dir(capsys) -> None:
    """A directory another user could write to must not host the socket."""

    sock_dir = Path(tempfile.gettempdir()) / f"a4i-t-{uuid.uuid4().hex[:8]}"
    sock_dir.mkdir()
    os.chmod(sock_dir, 0o777)
    try:
        assert daemon_main([str(sock_dir / "daemon.sock")]) == 1
        assert "must be mode 0700" in capsys.readouterr().err
        assert not (sock_dir / "daemon.sock").exists()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)
