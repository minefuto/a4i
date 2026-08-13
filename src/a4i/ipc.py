"""Client side of the CLI <-> daemon IPC: the daemon, as a caller sees it.

Commands connect to a per-user Unix domain socket and exchange newline-delimited
JSON messages. There is one function here per operation the daemon serves, and
each answers in the terms a caller thinks in: a typed reply, or the very
exception the daemon caught, rebuilt by :func:`a4i.errors.from_payload`. Nobody
outside handles a tag.

Whether a missing daemon is started is settled here rather than asked of the
caller, because for four of the six operations there is only one right answer.
:func:`status`, :func:`logout` and :func:`stop` never start one -- asking whether
a daemon is running must not bring one into being, and neither must ending a
session that is not there. :func:`login` always starts one, since a login with
nowhere to put the token is not a login. Only :func:`get` and :func:`post` take
the question, because there the answer differs by caller: a command is something
a person just typed, while the MCP server is launched by whatever started the
editor and a daemon spawned on that account is one nobody asked for.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Any, Literal, TypedDict

from a4i.errors import DaemonError, NoDaemonError, UnusableSocketError, from_payload

_SPAWN_TIMEOUT = 10.0
_CONNECT_RETRY = 0.05
# sizeof(sockaddr_un.sun_path): 104 on BSD/macOS, 108 on Linux. The smaller
# value is used everywhere, so a 104-107 byte path falls back on Linux too.
_SUN_PATH_MAX = 104
_FALLBACK_RUNTIME_DIR = "/tmp"  # noqa: S108 - fallback only
_SOCKET_NAME = "daemon.sock"
_DIR_MODE = 0o700


class LoginReply(TypedDict):
    """What the daemon holds once a login has landed."""

    user: str
    host: str
    refresh_timeout: float
    # What bounds each request the daemon will send, as opposed to
    # refresh_timeout above, which is how long the APIC will honour the token.
    timeout: float
    # Reported back rather than assumed from what was asked, so that a login that
    # did not ask for read-only still learns it landed on a daemon that is.
    read_only: bool


class EndReply(TypedDict):
    """What ending a session answers: whether the APIC could be told, and nothing else.

    The token is gone from the daemon either way, so this is a warning over a
    logout that succeeded rather than a failure to report.
    """

    apic_error: str | None


class LoggedOut(TypedDict):
    """A daemon running with no session in it."""

    logged_in: Literal[False]
    read_only: bool


class LoggedIn(TypedDict):
    """A daemon holding a session, and what it is holding."""

    logged_in: Literal[True]
    user: str
    host: str
    read_only: bool
    expires_in: float
    timeout: float


# Two shapes rather than one with holes in it: which fields are there follows
# from "logged_in", so a caller that has checked it has the rest.
Status = LoggedIn | LoggedOut


def socket_path() -> Path:
    """Return the per-user daemon socket path (0600, in a 0700 directory).

    ``XDG_RUNTIME_DIR`` is preferred, but falls back to ``/tmp`` when it is
    unset, is not a directory, or would yield a path that does not fit in a
    ``sockaddr_un``. The fallback path is always short enough, so this never
    fails.
    """

    name = f"a4i-{os.getuid()}"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        path = Path(runtime) / name / _SOCKET_NAME
        if len(os.fsencode(path)) < _SUN_PATH_MAX and os.path.isdir(runtime):
            return path
    return Path(_FALLBACK_RUNTIME_DIR) / name / _SOCKET_NAME


def check_socket_dir(directory: str | Path) -> None:
    """Raise :class:`DaemonError` unless *directory* is private and ours.

    The socket may sit under a world-writable ``/tmp``, where the socket name is
    predictable from the uid. Another user cannot delete our socket there (the
    sticky bit forbids it) but can create the path first, and a client that then
    connected would hand its APIC password to whoever is listening. Owning the
    enclosing directory is what rules that out, so both sides refuse a directory
    that is not a 0700 directory belonging to this user.

    A missing directory is not an error: it only means no daemon has started yet.
    """

    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise UnusableSocketError(f"{directory} is not a directory")
    if info.st_uid != os.getuid():
        raise UnusableSocketError(f"{directory} is owned by uid {info.st_uid}, not by you")
    if stat.S_IMODE(info.st_mode) != _DIR_MODE:
        raise UnusableSocketError(
            f"{directory} must be mode {_DIR_MODE:04o}, not {stat.S_IMODE(info.st_mode):04o}"
        )


def create_socket_dir(directory: str | Path) -> None:
    """Create the socket directory if needed, refusing one we do not own."""

    try:
        # Creating and then checking is race-free; checking and then creating is
        # not, because the loser of the race would adopt the winner's directory.
        os.mkdir(directory, _DIR_MODE)
    except FileExistsError:
        check_socket_dir(directory)
        return
    os.chmod(directory, _DIR_MODE)  # os.mkdir's mode is subject to the umask


def _connect(path: Path) -> socket.socket | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        sock.close()
        return None
    except OSError as exc:
        # ENAMETOOLONG, EACCES, ENOTDIR: the path itself cannot host a socket,
        # so spawning a daemon on it would not help either.
        sock.close()
        raise UnusableSocketError(f"cannot use the daemon socket {path}: {exc}") from None
    return sock


def _spawn_daemon(path: Path) -> None:
    """Start the daemon detached from this process and wait for it to listen."""

    # Imported here rather than at module scope: shell completion reaches this
    # module on every tab press but never spawns a daemon, and importing
    # subprocess costs more than the completion lookup it would pay for.
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-m", "a4i.daemon", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + _SPAWN_TIMEOUT
    while time.monotonic() < deadline:
        sock = _connect(path)
        if sock is not None:
            sock.close()
            return
        # The daemon's output is discarded, so a failure to bind would otherwise
        # only surface as a timeout. Connecting is tried first: a daemon that
        # loses the race for the socket exits 0 while the socket stays live.
        if proc.poll() is not None:
            raise DaemonError(f"the a4i daemon exited immediately (status {proc.returncode})")
        time.sleep(_CONNECT_RETRY)
    raise DaemonError("timed out waiting for the a4i daemon to start")


def _open(path: Path, *, autostart: bool) -> socket.socket:
    # Vet the directory before trusting whatever is listening inside it. Done
    # once here rather than in _connect, which the spawn loop calls repeatedly.
    check_socket_dir(path.parent)
    sock = _connect(path)
    if sock is None and autostart:
        _spawn_daemon(path)
        sock = _connect(path)
    if sock is None:
        raise NoDaemonError("no a4i daemon is running")
    return sock


def _exchange(sock: socket.socket, payload: bytes) -> bytes:
    try:
        with sock.makefile("rwb") as stream:
            stream.write(payload)
            stream.flush()
            return stream.readline()
    finally:
        sock.close()


def _request(op: str, args: dict[str, Any], *, autostart: bool) -> Any:
    """Send one request to the daemon and return its ``data`` payload.

    Raises whatever the daemon caught, rebuilt by
    :func:`a4i.errors.from_payload`, or a :class:`DaemonError` for a failure no
    daemon reported. If the daemon shuts down between connect and send (e.g. an
    idle-timeout race), the request is retried once with a freshly (auto)started
    daemon.
    """

    path = socket_path()
    payload = (json.dumps({"op": op, "args": args}) + "\n").encode()
    line = b""
    for attempt in range(2):
        sock = _open(path, autostart=autostart)
        try:
            line = _exchange(sock, payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionError):
            if attempt == 0 and autostart:
                continue
            raise DaemonError("lost connection to the a4i daemon") from None
        break

    if not line:
        raise DaemonError("empty response from the a4i daemon")
    reply = json.loads(line)
    if reply.get("ok"):
        return reply.get("data")
    raise from_payload(reply.get("error") or {})


# -- the operations --------------------------------------------------------


def login(
    host: str,
    user: str,
    password: str,
    *,
    verify: bool | str = True,
    timeout: float | None = None,
    read_only: bool = False,
) -> LoginReply:
    """Log in to ``host`` and leave the token in the daemon's memory.

    A daemon is started if none is running: a login with nowhere to put the
    token would be no login at all.

    ``timeout`` bounds every request the daemon will then send, in seconds. It is
    left out of the message entirely when None, so that the daemon's own default
    applies and this module needs no copy of it -- importing the one it would
    copy costs httpx2 on every command that goes through here.
    """

    args: dict[str, Any] = {
        "host": host,
        "user": user,
        "password": password,
        "verify": verify,
        "read_only": read_only,
    }
    if timeout is not None:
        args["timeout"] = timeout
    return _request("login", args, autostart=True)


def logout() -> EndReply:
    """End the session on the APIC and drop the token, leaving the daemon running.

    Starts nothing: with no daemon there is no session, which is the state this
    was asked to reach.
    """

    return _request("logout", {}, autostart=False)


def status() -> Status:
    """Say what the daemon is holding.

    Starts nothing, for the obvious reason: asking whether a daemon is running
    must not be what brings one into being.
    """

    return _request("status", {}, autostart=False)


def stop() -> EndReply:
    """End the session and the daemon with it. Starts nothing, as :func:`logout`."""

    return _request("stop", {}, autostart=False)


def get(
    target: str,
    kind: str,
    params: dict[str, str] | None,
    node: str | None,
    *,
    autostart: bool = True,
) -> Any:
    """GET through the daemon's session, and return the APIC's parsed response."""

    return _request(
        "get",
        {"target": target, "kind": kind, "params": params or {}, "node": node},
        autostart=autostart,
    )


def post(target: str, kind: str, body: str, *, autostart: bool = True) -> Any:
    """POST through the daemon's session, and return the APIC's parsed response."""

    return _request("post", {"target": target, "kind": kind, "body": body}, autostart=autostart)
