# A module of their own because both ends of the socket need them, and importing
# them from a4i._session would drag httpx2 into every `a4i get`.
#
# Not every exception here travels: DaemonError and the two below it are raised
# by the client before any daemon has answered, so they carry no tag and never
# appear in _WIRE.

from __future__ import annotations

from typing import Any


class A4iError(Exception):
    """Base of every error a request can fail with."""


class ApicError(A4iError):
    """Raised when the APIC returns an error MO or a failing HTTP status."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class NotLoggedInError(A4iError):
    """Raised when a request is attempted before a successful login."""


class NoFabricError(A4iError):
    pass


# What that error says, here rather than beside either raiser: the daemon raises
# it for a fabric it does not hold, and a4i._ipc for a daemon that is not there to
# hold one.
NO_FABRIC_MESSAGE = (
    "no fabric has been fetched: run 'a4i fetch' first\n"
    "(the cache is dropped by a post, a login, a logout, and a session expiry)"
)


# The daemon holds the read-only flag, so this is the answer to every writer
# sharing that session rather than a rule either entry point applies for itself.
class ReadOnlyError(A4iError):
    pass


class SessionExpiredError(A4iError):
    """Raised when the token lifetime elapsed and a re-login is required."""


# A failure no daemon reported: a connection lost mid-request, an empty reply, a
# daemon that would not start. What the daemon itself caught arrives rebuilt by
# from_payload.
class DaemonError(A4iError):
    pass


# A class of its own because a command that reads a failed request as "no daemon
# is running" must still report this one.
class UnusableSocketError(DaemonError):
    pass


class NoDaemonError(DaemonError):
    pass


# The tag each exception travels as. One dictionary, read in both directions
# below, so that an error cannot be given a way out and no way back.
_WIRE: dict[str, type[A4iError]] = {
    "apic": ApicError,
    "expired": SessionExpiredError,
    "no_fabric": NoFabricError,
    "not_logged_in": NotLoggedInError,
    "read_only": ReadOnlyError,
}
_TAGS: dict[type[A4iError], str] = {cls: tag for tag, cls in _WIRE.items()}

# What an exception with no tag of its own travels as. The daemon answers every
# failure, including the ones it never anticipated, so there has to be one.
_UNTAGGED = "error"


def to_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": _TAGS.get(type(exc), _UNTAGGED), "message": str(exc)}
    if isinstance(exc, ApicError):
        # The only detail that survives the crossing: a caller acts on the APIC's
        # code, where a status is about a request this side never made.
        payload["code"] = exc.code
    return payload


def from_payload(payload: dict[str, Any]) -> A4iError:
    # An unknown tag comes back as a plain DaemonError: a daemon left running
    # across an upgrade may classify something this command has never heard of.
    message = str(payload.get("message") or "unknown daemon error")
    cls = _WIRE.get(str(payload.get("type", "")))
    if cls is None:
        return DaemonError(message)
    if cls is ApicError:
        return ApicError(message, code=payload.get("code"))
    return cls(message)
