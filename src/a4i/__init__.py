"""a4i - a CLI for the Cisco ACI (APIC) REST API, and the library behind it.

    import a4i

    with a4i.Client("apic1.example.com") as client:
        client.login("admin", password)
        data = client.get("fvTenant", kind="class", query_target="subtree")

    async with a4i.AsyncClient("apic1.example.com") as client:
        await client.login("admin", password)
        data = await client.get("fvTenant", kind="class", query_target="subtree")

The names below are resolved on first use rather than imported here. Reaching
:class:`~a4i.client.Client` pulls in the whole request and comparison stack,
which costs more than a whole shell completion -- and completion runs through
this package on every tab press. ``__version__`` is held back for the same
reason: reading it takes importlib.metadata, which alone costs more than the
completion it would be paid for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Redundant aliases: these exist for a type checker and an IDE, which cannot
    # follow __getattr__ below. Nothing is imported at run time.
    __version__: str

    from a4i.client import AsyncClient as AsyncClient
    from a4i.client import Client as Client
    from a4i.errors import A4iError as A4iError
    from a4i.errors import ApicError as ApicError
    from a4i.errors import NotLoggedInError as NotLoggedInError
    from a4i.errors import ReadOnlyError as ReadOnlyError
    from a4i.errors import SessionExpiredError as SessionExpiredError
    from a4i.mo import Change as Change

# What __version__ reads when the package is not installed, which is what an
# uninstalled source tree looks like to importlib.metadata.
_FALLBACK_VERSION = "0.0.1"

# Public name -> the module it lives in.
_EXPORTS = {
    "Client": "a4i.client",
    "AsyncClient": "a4i.client",
    "Change": "a4i.mo",
    "A4iError": "a4i.errors",
    "ApicError": "a4i.errors",
    "NotLoggedInError": "a4i.errors",
    "ReadOnlyError": "a4i.errors",
    "SessionExpiredError": "a4i.errors",
}

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str) -> object:
    if name == "__version__":
        # The version is whatever the build recorded, which hatch-vcs takes from
        # the git tag, so nothing here has to be bumped for a release.
        from importlib.metadata import PackageNotFoundError, version

        resolved: object
        try:
            resolved = version("a4i")
        except PackageNotFoundError:
            resolved = _FALLBACK_VERSION
        globals()[name] = resolved
        return resolved

    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module), name)
    # Cached in the module's own namespace, so the lookup happens once.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
