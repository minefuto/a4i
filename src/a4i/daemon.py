"""Per-user daemon that holds the APIC session token in memory.

The daemon listens on a Unix domain socket and serves one request per
connection. It owns a single :class:`~a4i.session.Session`; the token lives only
in this process's memory and is never written to disk. The session is refreshed
lazily on command activity and expires on its own once idle past its lifetime.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from collections.abc import Callable
from typing import Any

from a4i import query
from a4i.errors import ReadOnlyError
from a4i.ipc import DaemonError, create_socket_dir
from a4i.session import ApicError, NotLoggedInError, Session, SessionExpiredError

# What a POST is told when the session was logged in read-only. It names the way
# out, because there is exactly one and it is not obvious: a fresh login does not
# clear the flag.
READ_ONLY_MESSAGE = (
    "this session is read-only (logged in with --read-only); "
    "run 'a4i daemon stop' and log in again to write"
)

# How long the daemon lingers with no active session before exiting.
IDLE_GRACE = 300.0
# Wake period for the accept loop so lifecycle checks run regularly.
ACCEPT_TIMEOUT = 15.0


class Daemon:
    def __init__(
        self,
        sock_path: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        session_factory: Callable[..., Session] = Session,
    ) -> None:
        self._path = sock_path
        self._clock = clock
        self._session_factory = session_factory
        self._session: Session | None = None
        # Sticky for the daemon's whole life, never cleared by a logout or by a
        # later login that does not ask for it. A flag a fresh login could drop
        # would be no guarantee at all: the point of it is that nothing reaching
        # this daemon can write, and "nothing" has to include the next login.
        self._read_only = False
        self._last_activity = clock()
        self._running = True
        self._server: socket.socket | None = None

    # -- lifecycle --------------------------------------------------------

    def _bind(self) -> bool:
        """Bind the socket. Return False if another daemon already owns it."""

        # Unlinking a stale socket below is only safe in a directory no other
        # user can write to, which is what create_socket_dir guarantees.
        create_socket_dir(os.path.dirname(self._path))
        if os.path.exists(self._path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(self._path)
            except (ConnectionRefusedError, FileNotFoundError):
                os.unlink(self._path)  # stale socket
            else:
                probe.close()
                return False
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._path)
        os.chmod(self._path, 0o600)
        server.listen(16)
        server.settimeout(ACCEPT_TIMEOUT)
        self._server = server
        return True

    def serve(self) -> None:
        if not self._bind():
            return  # another daemon is already running
        assert self._server is not None
        server = self._server
        try:
            while self._running:
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    # Lifecycle (expiry + idle shutdown) is evaluated only on the
                    # idle tick, never mid-request, so a command in flight is never
                    # cut off by a shutdown decision.
                    self._check_lifecycle()
                    continue
                with conn:
                    self._handle(conn)
        finally:
            self._cleanup()

    def _check_lifecycle(self) -> None:
        if self._session is not None and self._session.is_expired():
            # An expired token has nothing left to end on the APIC, so the
            # session is only closed here, never logged out.
            self._session.close()
            self._session = None
        if self._session is None and self._clock() - self._last_activity > IDLE_GRACE:
            self._running = False

    def _cleanup(self) -> None:
        if self._session is not None:
            self._session.close()
        if self._server is not None:
            self._server.close()
        if os.path.exists(self._path):
            os.unlink(self._path)
        # The enclosing directory is left in place on purpose: while it exists,
        # nobody else can claim its name, so the next daemon starts on a
        # directory that is already known to be ours.

    # -- request handling -------------------------------------------------

    def _handle(self, conn: socket.socket) -> None:
        with conn.makefile("rwb") as stream:
            line = stream.readline()
            if not line:
                return
            try:
                message = json.loads(line)
                data = self._dispatch(message["op"], message.get("args", {}))
                reply: dict[str, Any] = {"ok": True, "data": data}
            except Exception as exc:  # noqa: BLE001 - reported back to the client
                reply = {"ok": False, "error": _error_payload(exc)}
            stream.write((json.dumps(reply) + "\n").encode())
            stream.flush()

    def _dispatch(self, op: str, args: dict[str, Any]) -> Any:
        self._last_activity = self._clock()
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise ValueError(f"unknown op: {op}")
        return handler(args)

    # -- ops --------------------------------------------------------------

    def _op_login(self, args: dict[str, Any]) -> Any:
        if self._session is not None:
            self._session.close()
        self._read_only = self._read_only or bool(args.get("read_only"))
        self._session = self._session_factory(args["host"], verify=args.get("verify", True))
        self._session.login(args["user"], args["password"])
        return {
            "user": self._session.user,
            "host": self._session.base_url,
            "refresh_timeout": self._session.refresh_timeout,
            # Reported back rather than assumed from what was asked, so that a
            # login that did not ask for read-only still learns it landed on a
            # daemon that is.
            "read_only": self._read_only,
        }

    def _end_session(self) -> str | None:
        """Log out of the APIC and drop the session. Return the APIC failure, if any.

        The session goes either way -- the token is gone from here whether or not
        the APIC could be told -- so the failure is reported back rather than
        raised, and the client turns it into a warning over a successful logout.
        """

        apic_error: str | None = None
        if self._session is not None:
            try:
                self._session.logout()
            except ApicError as exc:
                apic_error = str(exc)
            self._session.close()
            self._session = None
        return apic_error

    def _op_logout(self, args: dict[str, Any]) -> Any:
        return {"logged_out": True, "apic_error": self._end_session()}

    def _op_status(self, args: dict[str, Any]) -> Any:
        if self._session is None or not self._session.logged_in:
            return {"logged_in": False, "read_only": self._read_only}
        return {
            "logged_in": True,
            "user": self._session.user,
            "host": self._session.base_url,
            "read_only": self._read_only,
            "expires_in": max(
                0.0, self._session.refresh_timeout - self._session.seconds_since_auth()
            ),
        }

    def _op_get(self, args: dict[str, Any]) -> Any:
        session = self._require_session()
        return session.get(
            query.build_path(args["target"], args["kind"]),
            args.get("params") or None,
            host=args.get("node"),
        )

    def _op_post(self, args: dict[str, Any]) -> Any:
        # Checked before the session is required, so that a read-only daemon says
        # so whether or not anyone has logged in yet.
        if self._read_only:
            raise ReadOnlyError(READ_ONLY_MESSAGE)
        session = self._require_session()
        return session.post(query.build_path(args["target"], args["kind"]), args["body"])

    def _op_stop(self, args: dict[str, Any]) -> Any:
        # Logging out here rather than in _cleanup, so that the reply carries
        # what the APIC made of it; _cleanup then finds nothing left to end.
        self._running = False
        return {"stopping": True, "apic_error": self._end_session()}

    def _require_session(self) -> Session:
        if self._session is None or not self._session.logged_in:
            raise NotLoggedInError("not logged in")
        return self._session


def _error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ApicError):
        return {"type": "apic", "message": str(exc), "code": exc.code}
    if isinstance(exc, SessionExpiredError):
        return {"type": "expired", "message": str(exc)}
    if isinstance(exc, NotLoggedInError):
        return {"type": "not_logged_in", "message": str(exc)}
    if isinstance(exc, ReadOnlyError):
        return {"type": "read_only", "message": str(exc)}
    return {"type": "error", "message": str(exc)}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m a4i.daemon <socket-path>", file=sys.stderr)
        return 2
    try:
        Daemon(argv[0]).serve()
    except DaemonError as exc:
        # Refusing an unsafe socket directory is a normal outcome, not a crash.
        print(f"a4i.daemon: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
