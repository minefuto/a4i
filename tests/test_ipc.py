from __future__ import annotations

import os
import shutil
import socket as socket_mod
import stat
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from a4i import ipc
from a4i.ipc import DaemonError

SOCK = "daemon.sock"


@pytest.fixture
def short_dir():
    """An existing 0700 directory short enough to hold an AF_UNIX socket path.

    pytest's ``tmp_path`` is itself over the sun_path limit on macOS, which
    would make the cases below fall back for the wrong reason.
    """

    path = Path(tempfile.gettempdir()) / f"a4i-t-{uuid.uuid4().hex[:8]}"
    path.mkdir(mode=0o700)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _fallback() -> Path:
    return Path("/tmp") / f"a4i-{os.getuid()}" / SOCK


# -- socket_path -----------------------------------------------------------


def test_socket_path_uses_runtime_dir(monkeypatch, short_dir) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))
    assert ipc.socket_path() == short_dir / f"a4i-{os.getuid()}" / SOCK


def test_socket_path_falls_back_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert ipc.socket_path() == _fallback()


def test_socket_path_falls_back_when_empty(monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    assert ipc.socket_path() == _fallback()


def test_socket_path_falls_back_when_too_long(monkeypatch, short_dir) -> None:
    # An existing directory, so only the length can rule it out.
    runtime = short_dir / ("d" * 120)
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert ipc.socket_path() == _fallback()


def test_socket_path_falls_back_when_missing(monkeypatch, short_dir) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir / "missing"))
    assert ipc.socket_path() == _fallback()


def test_socket_path_falls_back_when_not_a_directory(monkeypatch, short_dir) -> None:
    runtime = short_dir / "file"
    runtime.write_text("")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert ipc.socket_path() == _fallback()


def test_socket_path_fits_a_sockaddr_un(monkeypatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert len(os.fsencode(ipc.socket_path())) < ipc._SUN_PATH_MAX


# -- socket directory ownership --------------------------------------------


def test_check_socket_dir_accepts_our_private_dir(short_dir) -> None:
    ipc.check_socket_dir(short_dir)


def test_check_socket_dir_accepts_a_missing_dir(short_dir) -> None:
    # No directory only means no daemon has started yet.
    ipc.check_socket_dir(short_dir / "missing")


def test_check_socket_dir_rejects_a_file(short_dir) -> None:
    target = short_dir / "file"
    target.write_text("")
    with pytest.raises(DaemonError, match="is not a directory"):
        ipc.check_socket_dir(target)


def test_check_socket_dir_rejects_a_loose_mode(short_dir) -> None:
    target = short_dir / "loose"
    target.mkdir()
    os.chmod(target, 0o755)  # mkdir's mode is masked by the umask
    with pytest.raises(DaemonError, match="must be mode 0700, not 0755"):
        ipc.check_socket_dir(target)


def test_check_socket_dir_rejects_another_owner(monkeypatch, short_dir) -> None:
    # Stand in for a directory another user created first.
    monkeypatch.setattr(os, "getuid", lambda: os.stat(short_dir).st_uid + 1)
    with pytest.raises(DaemonError, match="is owned by uid"):
        ipc.check_socket_dir(short_dir)


def test_create_socket_dir_creates_a_private_dir(short_dir) -> None:
    target = short_dir / "new"
    ipc.create_socket_dir(target)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700


def test_create_socket_dir_creates_a_private_dir_under_a_loose_umask(short_dir) -> None:
    target = short_dir / "new"
    old = os.umask(0o077)
    try:
        os.umask(0o000)
        ipc.create_socket_dir(target)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700


def test_create_socket_dir_accepts_its_own_dir_again(short_dir) -> None:
    target = short_dir / "new"
    ipc.create_socket_dir(target)
    ipc.create_socket_dir(target)


def test_create_socket_dir_rejects_a_squatted_dir(short_dir) -> None:
    # What another user could leave behind in a world-writable /tmp.
    target = short_dir / "squatted"
    target.mkdir()
    os.chmod(target, 0o777)
    with pytest.raises(DaemonError, match="must be mode 0700"):
        ipc.create_socket_dir(target)


def test_request_refuses_a_socket_in_a_squatted_dir(monkeypatch, short_dir) -> None:
    """A client must not hand its password to a socket in someone else's dir."""

    squatted = short_dir / "squatted"
    squatted.mkdir()
    os.chmod(squatted, 0o777)
    sock_path = squatted / SOCK
    # Bound but not listening: if the directory check ever regressed, the
    # request would fail as "no daemon" rather than hang on a live socket.
    server = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    server.bind(str(sock_path))
    monkeypatch.setattr(ipc, "socket_path", lambda: sock_path)
    try:
        with pytest.raises(DaemonError, match="must be mode 0700"):
            ipc.request("login", autostart=False, host="h", user="u", password="secret")
    finally:
        server.close()


# -- unusable socket paths -------------------------------------------------


def test_connect_reports_too_long_path_as_daemon_error() -> None:
    with pytest.raises(DaemonError, match="cannot use the daemon socket"):
        ipc._connect(Path("/tmp") / ("x" * 200))


def test_request_reports_too_long_path_as_daemon_error(monkeypatch, short_dir) -> None:
    monkeypatch.setattr(ipc, "socket_path", lambda: short_dir / ("x" * 200))
    with pytest.raises(DaemonError, match="cannot use the daemon socket"):
        ipc.request("status")


def test_spawn_daemon_fails_fast_when_daemon_cannot_bind() -> None:
    # The daemon dies before binding; it must not be waited out for
    # _SPAWN_TIMEOUT, whose whole duration is what this used to cost.
    started = time.monotonic()
    with pytest.raises(DaemonError, match="exited immediately"):
        ipc._spawn_daemon(Path("/nonexistent-a4i-dir/sub") / SOCK)
    assert time.monotonic() - started < ipc._SPAWN_TIMEOUT / 2
