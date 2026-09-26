"""Client side of the CLI <-> daemon IPC: the daemon, as a caller sees it.

One function per operation the daemon serves, each answering in the terms a
caller thinks in: a typed reply, or the very exception the daemon caught, so
nobody outside handles a tag. Whether a missing daemon is started is settled
here rather than asked of the caller, except where the right answer differs by
caller: a command is something a person just typed, where the MCP server is
launched by whatever started the editor.
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

from a4i._errors import (
    NO_FABRIC_MESSAGE,
    DaemonError,
    NoDaemonError,
    NoFabricError,
    UnusableSocketError,
    from_payload,
)

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
    """What ending a session answers: whether the APIC could be told.

    The token is gone from the daemon either way, so this is a warning over a
    logout that succeeded rather than a failure to report.
    """

    apic_error: str | None


class FabricHeld(TypedDict):
    """The fetched fabric a daemon is holding, if it is holding one."""

    count: int
    # Elapsed rather than a timestamp: the daemon measures on a monotonic clock,
    # which has no meaning in another process, and what a reader wants is how old
    # the comparison about to run will be.
    fetched_ago: float


class LoggedOut(TypedDict):
    """A daemon running with no session in it."""

    logged_in: Literal[False]
    read_only: bool
    fabric: FabricHeld | None


class LoggedIn(TypedDict):
    """A daemon holding a session, and what it is holding."""

    logged_in: Literal[True]
    user: str
    host: str
    read_only: bool
    expires_in: float
    timeout: float
    fabric: FabricHeld | None


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

    The socket may sit under a world-writable ``/tmp``, where its name is
    predictable from the uid: another user cannot delete it (the sticky bit
    forbids it) but can create the path first, and a client that then connected
    would hand its APIC password to whoever is listening. Both sides check.

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
        [sys.executable, "-m", "a4i._daemon", str(path)],
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
    :func:`a4i._errors.from_payload`, or a :class:`DaemonError` for a failure no
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

    ``timeout`` bounds every request the daemon will then send, in seconds, and
    is left out of the message entirely when None, so that this module needs no
    copy of the default -- importing the one it would copy costs httpx2 on every
    command that goes through here.
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
    """Say what the daemon is holding. Starts nothing, as :func:`logout`."""

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


def fetch(*, autostart: bool = True) -> dict[str, Any]:
    """Read the whole of uni into the daemon and return how many MOs that was.

    The body itself stays there: it is what :func:`fabric` hands to the next
    comparison, and nothing this side does with it would survive the process.
    """

    return _request("fetch", {}, autostart=autostart)


def fabric() -> Any:
    """Return the fabric a fetch left in the daemon, as one body.

    Starts nothing, and a daemon that is not running is answered as one holding
    no fabric would answer: the next move is a fetch either way.

    Raises :class:`~a4i._errors.NoFabricError` when nothing has been fetched.
    """

    try:
        return _request("fabric", {}, autostart=False)
    except NoDaemonError:
        raise NoFabricError(NO_FABRIC_MESSAGE) from None
