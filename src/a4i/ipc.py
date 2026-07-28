"""Client side of the CLI <-> daemon IPC.

Commands connect to a per-user Unix domain socket and exchange newline-delimited
JSON messages. If no daemon is listening, one is spawned automatically and
detached from the calling process -- unless the caller turns ``autostart`` off,
as the requests that only inspect a daemon already running do.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Any

from a4i.errors import A4iError

_SPAWN_TIMEOUT = 10.0
_CONNECT_RETRY = 0.05
# sizeof(sockaddr_un.sun_path): 104 on BSD/macOS, 108 on Linux. The smaller
# value is used everywhere, so a 104-107 byte path falls back on Linux too.
_SUN_PATH_MAX = 104
_FALLBACK_RUNTIME_DIR = "/tmp"  # noqa: S108 - fallback only
_SOCKET_NAME = "daemon.sock"
_DIR_MODE = 0o700


class DaemonError(A4iError):
    """Raised when a request fails, by the daemon or before it is even reached.

    ``type`` carries the daemon's own classification (see
    :func:`a4i.daemon._error_payload`), plus two this client raises before any
    daemon is reached: ``"socket"`` for a socket path it refuses to use, and
    ``"no_daemon"`` for nothing listening on a usable one. Commands that treat a
    failed request as "no daemon is running" must still report the first.
    """

    def __init__(self, message: str, *, type: str = "error", code: str | None = None) -> None:
        super().__init__(message)
        self.type = type
        self.code = code


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
        raise DaemonError(f"{directory} is not a directory", type="socket")
    if info.st_uid != os.getuid():
        raise DaemonError(f"{directory} is owned by uid {info.st_uid}, not by you", type="socket")
    if stat.S_IMODE(info.st_mode) != _DIR_MODE:
        raise DaemonError(
            f"{directory} must be mode {_DIR_MODE:04o}, not {stat.S_IMODE(info.st_mode):04o}",
            type="socket",
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
        raise DaemonError(f"cannot use the daemon socket {path}: {exc}", type="socket") from None
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
        raise DaemonError("no a4i daemon is running", type="no_daemon")
    return sock


def _exchange(sock: socket.socket, payload: bytes) -> bytes:
    try:
        with sock.makefile("rwb") as stream:
            stream.write(payload)
            stream.flush()
            return stream.readline()
    finally:
        sock.close()


def request(op: str, *, autostart: bool = True, **args: Any) -> Any:
    """Send one request to the daemon and return its ``data`` payload.

    Raises :class:`DaemonError` if the daemon reports a failure. If the daemon
    shuts down between connect and send (e.g. an idle-timeout race), the request
    is retried once with a freshly (auto)started daemon.
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
    err = reply.get("error", {})
    raise DaemonError(
        err.get("message", "unknown daemon error"),
        type=err.get("type", "error"),
        code=err.get("code"),
    )
