"""The exceptions every entry point raises.

They live in a module of their own rather than beside the code that raises them,
because both ends of the socket need them. :mod:`a4i.transport` turns a daemon's
error reply back into one of these, and importing them from :mod:`a4i.session`
would drag httpx2 into every ``a4i get`` -- a cost the daemon already pays and the
command should not.
"""

from __future__ import annotations


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
