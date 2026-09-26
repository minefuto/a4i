"""a4i - a CLI for the Cisco ACI (APIC) REST API, and the library behind it.

    import a4i

    with a4i.Client("apic1.example.com") as client:
        client.login("admin", password)
        data = client.get("fvTenant", kind="class", query_target="subtree")

    async with a4i.AsyncClient("apic1.example.com") as client:
        await client.login("admin", password)
        data = await client.get("fvTenant", kind="class", query_target="subtree")

The names below are resolved on first use rather than imported here, ``__version__``
included: shell completion runs through this package on every tab press, and
reaching :class:`~a4i._client.Client` or importlib.metadata costs more than the
whole completion would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Redundant aliases: these exist for a type checker and an IDE, which cannot
    # follow __getattr__ below. Nothing is imported at run time.
    __version__: str

    from a4i._client import AsyncClient as AsyncClient
    from a4i._client import Client as Client
    from a4i._diff import compare as diff  # noqa: F401
    from a4i._dry_run import check as dry_run  # noqa: F401
    from a4i._errors import A4iError as A4iError
    from a4i._errors import ApicError as ApicError
    from a4i._errors import NotLoggedInError as NotLoggedInError
    from a4i._errors import SessionExpiredError as SessionExpiredError
    from a4i._merge import UndescribedError as UndescribedError
    from a4i._merge import merge as merge
    from a4i._mo import Change as Change
    from a4i._plan import create as plan  # noqa: F401

# What __version__ reads when the package is not installed, which is what an
# uninstalled source tree looks like to importlib.metadata.
_FALLBACK_VERSION = "0.0.1"

# Public name -> the module it lives in, and its name there.
_EXPORTS = {
    "Client": ("a4i._client", "Client"),
    "AsyncClient": ("a4i._client", "AsyncClient"),
    "Change": ("a4i._mo", "Change"),
    "merge": ("a4i._merge", "merge"),
    "diff": ("a4i._diff", "compare"),
    "plan": ("a4i._plan", "create"),
    "dry_run": ("a4i._dry_run", "check"),
    "A4iError": ("a4i._errors", "A4iError"),
    "ApicError": ("a4i._errors", "ApicError"),
    "NotLoggedInError": ("a4i._errors", "NotLoggedInError"),
    "SessionExpiredError": ("a4i._errors", "SessionExpiredError"),
    "UndescribedError": ("a4i._merge", "UndescribedError"),
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

    export = _EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module, attribute = export
    value = getattr(importlib.import_module(module), attribute)
    # Cached in the module's own namespace, so the lookup happens once.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
