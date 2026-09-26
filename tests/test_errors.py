from __future__ import annotations

import pytest

from a4i import _errors as errors
from a4i._errors import (
    A4iError,
    ApicError,
    DaemonError,
    NoDaemonError,
    NotLoggedInError,
    ReadOnlyError,
    SessionExpiredError,
    UnusableSocketError,
    from_payload,
    to_payload,
)


def _subclasses(cls: type) -> list[type]:
    found = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_subclasses(sub))
    return found


# -- the round trip --------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ApicError("boom"),
        SessionExpiredError("session expired; please login again"),
        NotLoggedInError("not logged in"),
        ReadOnlyError("this session is read-only"),
    ],
)
def test_an_error_that_travels_comes_back_as_itself(exc) -> None:
    restored = from_payload(to_payload(exc))
    assert type(restored) is type(exc)
    assert str(restored) == str(exc)


def test_the_apic_code_survives_the_crossing() -> None:
    # The one detail a caller acts on, so it is the one that travels.
    restored = from_payload(to_payload(ApicError("boom", code="122", status=400)))
    assert isinstance(restored, ApicError)
    assert restored.code == "122"


def test_an_exception_with_no_tag_of_its_own_travels_as_a_daemon_error() -> None:
    # The daemon answers every failure, including the ones it never anticipated.
    restored = from_payload(to_payload(RuntimeError("something else entirely")))
    assert type(restored) is DaemonError
    assert str(restored) == "something else entirely"


# A daemon left running across an upgrade may classify something new.
def test_a_tag_this_client_does_not_know_keeps_what_the_daemon_said() -> None:
    restored = from_payload({"type": "invented_later", "message": "a newer daemon says so"})
    assert type(restored) is DaemonError
    assert str(restored) == "a newer daemon says so"


def test_an_error_payload_with_nothing_in_it_still_raises_something() -> None:
    assert str(from_payload({})) == "unknown daemon error"


# -- nothing added by halves -----------------------------------------------


# Checking the round trip does not catch an error wired in one direction only -- a class
# nobody listed is a class no round trip visits. So this walks the hierarchy instead:
# every A4iError is either in _WIRE, and therefore crosses the socket both ways, or is
# one of the client's own and never crosses.
def test_every_error_either_travels_or_is_the_clients_own() -> None:
    for cls in _subclasses(A4iError):
        travels = cls in errors._WIRE.values()
        clients_own = issubclass(cls, DaemonError)
        assert travels != clients_own, (
            f"{cls.__name__} is in neither camp: put it in a4i._errors._WIRE if the daemon "
            f"raises it, or under DaemonError if this client does"
        )


def test_the_clients_own_errors_do_not_travel() -> None:
    # They are raised before a daemon has answered, so there is nothing on the
    # far side to have classified them.
    for exc in (
        DaemonError("lost connection"),
        UnusableSocketError("bad dir"),
        NoDaemonError("no"),
    ):
        assert to_payload(exc)["type"] == errors._UNTAGGED


def test_a_tag_names_one_class_and_a_class_one_tag() -> None:
    assert len(errors._TAGS) == len(errors._WIRE)
