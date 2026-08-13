"""The exceptions every entry point raises, and how one crosses the socket.

They live in a module of their own rather than beside the code that raises them,
because both ends of the socket need them: the daemon flattens what it caught
with :func:`to_payload`, and the client builds it back with
:func:`from_payload`. Importing them from :mod:`a4i.session` instead would drag
httpx2 into every ``a4i get`` -- a cost the daemon already pays and the command
should not.

The two functions sit next to each other on purpose. What travels is a tag, and
a tag is worth something only insofar as both directions agree on it; keeping
the writing and the reading in one place is what makes a new one impossible to
add by halves.

Not every exception here travels. :class:`DaemonError` and the two below it are
raised by the client before any daemon has answered, so they carry no tag and
never appear in ``_WIRE``. That is the whole division: an error either travels,
or it is the client's own.
"""

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


class ReadOnlyError(A4iError):
    """Raised when a POST is attempted on a session logged in read-only.

    The daemon holds the flag, so this is the answer to every writer sharing that
    session -- the MCP server and the command line alike -- and not a rule either
    of them applies for itself.
    """


class SessionExpiredError(A4iError):
    """Raised when the token lifetime elapsed and a re-login is required."""


class DaemonError(A4iError):
    """Raised when a request to the daemon fails for a reason no daemon reported.

    A failure the daemon itself caught arrives as one of the typed exceptions
    above, rebuilt by :func:`from_payload`. This one is for what goes wrong on
    the way instead: a connection lost mid-request, an empty reply, a daemon that
    would not start -- and, in the two below, the two the client settles for
    itself before it has spoken to anything.
    """


class UnusableSocketError(DaemonError):
    """Raised for a socket path this client refuses to use.

    Nothing was sent: the path is turned down because the directory holding it is
    not a private one of ours, or because the path itself cannot host a socket at
    all. Commands that read a failed request as "no daemon is running" must still
    report this one, which is why it is a class of its own rather than one more
    way for a request to come back empty.
    """


class NoDaemonError(DaemonError):
    """Raised when nothing is listening on a usable socket, and none was started."""


# The tag each exception travels as. One dictionary, read in both directions
# below, so that an error cannot be given a way out and no way back.
_WIRE: dict[str, type[A4iError]] = {
    "apic": ApicError,
    "expired": SessionExpiredError,
    "not_logged_in": NotLoggedInError,
    "read_only": ReadOnlyError,
}
_TAGS: dict[type[A4iError], str] = {cls: tag for tag, cls in _WIRE.items()}

# What an exception with no tag of its own travels as. The daemon answers every
# failure, including the ones it never anticipated, so there has to be one.
_UNTAGGED = "error"


def to_payload(exc: Exception) -> dict[str, Any]:
    """Flatten an exception into the error payload a daemon replies with."""

    payload: dict[str, Any] = {"type": _TAGS.get(type(exc), _UNTAGGED), "message": str(exc)}
    if isinstance(exc, ApicError):
        # The only detail that survives the crossing: a caller acts on the APIC's
        # code, where a status is about a request this side never made.
        payload["code"] = exc.code
    return payload


def from_payload(payload: dict[str, Any]) -> A4iError:
    """Build back the exception an error payload describes.

    A tag this client does not know comes back as a plain :class:`DaemonError`
    carrying the message the daemon wrote. That is not a failure to handle: a
    daemon left running across an upgrade may classify something this command has
    never heard of, and what it said is still worth reporting.
    """

    message = str(payload.get("message") or "unknown daemon error")
    cls = _WIRE.get(str(payload.get("type", "")))
    if cls is None:
        return DaemonError(message)
    if cls is ApicError:
        return ApicError(message, code=payload.get("code"))
    return cls(message)
