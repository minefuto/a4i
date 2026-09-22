"""The ACI REST API as a Python object.

What a get or a post means lives here; how the request travels is
:mod:`a4i.transport`'s business, so the CLI and a library caller send the very
same request. A client made from a host owns its session and keeps the token in
this process's memory. :class:`AsyncClient` is spelled out separately rather
than shared with :class:`Client`, because what the two have in common already
lives outside both of them and what is left is the awaiting.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any

from a4i import dry_run, merge, mo, query
from a4i.errors import ApicError
from a4i.transport import AsyncDirectTransport, AsyncTransport, DirectTransport, Transport
from a4i.validate import read_body

if TYPE_CHECKING:
    from a4i.session import AsyncSession, Session

# What a comparison needs to see of a subtree: the whole of it, because the body
# may reach into it, and only the settable properties, because only those change.
_CURRENT_STATE = {"rsp-subtree": "full", "rsp-prop-include": "config-only"}

# "config-only" rather than "naming-only" because this list decides what gets
# walked: uni's runtime children would only be reported extra. It returns the DNs
# all the same.
_UNI_CHILDREN = {"query-target": "children", "rsp-prop-include": "config-only"}

# A browse, so it leaves nothing out -- deliberately not _UNI_CHILDREN.
_CHILDREN = {"query-target": "children", "rsp-prop-include": "naming-only"}

_NO_SESSION = "this client has no session of its own"


class Client:
    """A connection to one APIC.

    ``transport`` replaces the session this client would otherwise build. The
    CLI passes a :class:`~a4i.transport.DaemonTransport` so that its requests go
    through the daemon holding the token; the session methods below then do not
    apply. A transport that owns a session of its own -- a
    :class:`~a4i.transport.DirectTransport` around a
    :class:`~a4i.session.Session` built by hand -- hands it over, which is the
    way in for a session this constructor cannot express. ``verify`` and
    ``timeout`` describe the session this constructor would build, so a
    ``transport`` that brings its own settles both of them itself.

    ``timeout`` bounds every request this client sends, in seconds; None is not
    "no timeout" but "whichever :data:`~a4i.session.DEFAULT_TIMEOUT` says", named
    that way so the default lives in one place and is read only once a session is
    actually being built.
    """

    def __init__(
        self,
        host: str | None = None,
        *,
        verify: bool | str = True,
        timeout: float | None = None,
        transport: Transport | None = None,
    ) -> None:
        if transport is None:
            if host is None:
                raise TypeError("a host is required")
            # Imported here rather than at module scope: a CLI command reaches
            # this module with a transport of its own, and importing httpx2 costs
            # more than the whole command that would never use it.
            from a4i.session import DEFAULT_TIMEOUT, Session

            transport = DirectTransport(
                Session(
                    host,
                    verify=verify,
                    timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
                )
            )
        self._transport = transport
        self._session: Session | None = getattr(transport, "session", None)

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- authentication ---------------------------------------------------

    def login(self, user: str, password: str) -> None:
        """Authenticate to the APIC and hold the token in memory."""

        self._own_session().login(user, password)

    def logout(self) -> None:
        """End the session on the APIC and drop the token.

        The token is dropped whether or not the APIC could be told, so this
        client is logged out even when it raises: an unreachable or unhappy APIC
        surfaces as an :class:`~a4i.errors.ApicError` after the fact, leaving
        only the APIC's own copy of the session to expire on its own.
        """

        self._own_session().logout()

    def refresh(self) -> None:
        """Refresh the token now.

        Requests refresh it on their own once half its lifetime has elapsed, so
        this is only needed to keep a session alive across a quiet stretch.
        """

        self._own_session().refresh()

    def close(self) -> None:
        """Close the connections. A transport holding no session of its own has none."""

        if self._session is not None:
            self._session.close()

    @property
    def logged_in(self) -> bool:
        return self._session is not None and self._session.logged_in

    # -- requests ---------------------------------------------------------

    def get(
        self,
        target: str,
        *,
        kind: query.Kind,
        query_target: str | None = None,
        target_subtree_class: str | Sequence[str] | None = None,
        query_target_filter: str | None = None,
        rsp_subtree: str | None = None,
        rsp_subtree_class: str | Sequence[str] | None = None,
        rsp_subtree_filter: str | None = None,
        rsp_subtree_include: str | Sequence[str] | None = None,
        rsp_prop_include: str | None = None,
        order_by: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        node: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """GET a class or an MO, and return the APIC's response as it came.

        ``kind`` says what ``target`` is: ``"class"`` for a class name
        (``fvTenant``) or ``"mo"`` for a DN (``uni/tn-common``). Every other
        argument is named after the ACI query parameter it sets. ``node`` sends
        the query straight to a fabric switch's local MIT with the same token;
        the session still lives on the APIC. Raises :class:`ValueError` on a
        value ACI does not define.
        """

        built = query.build_get_params(
            query_target=query_target,
            target_subtree_class=target_subtree_class,
            query_target_filter=query_target_filter,
            rsp_subtree=rsp_subtree,
            rsp_subtree_class=rsp_subtree_class,
            rsp_subtree_filter=rsp_subtree_filter,
            rsp_subtree_include=rsp_subtree_include,
            rsp_prop_include=rsp_prop_include,
            order_by=order_by,
            page=page,
            page_size=page_size,
            params=params,
        )
        return self._transport.get(target, kind, built, node)

    def list_children(self, dn: str, *, node: str | None = None) -> list[str]:
        """Return the DNs of the MOs directly under ``dn``, sorted.

        The DNs come back bare, so one of them is what :meth:`get` takes as its
        target. ``node`` lists from a fabric switch's local MIT instead, as on
        :meth:`get`.
        """

        data = self._transport.get(dn, "mo", dict(_CHILDREN), node)
        return mo.top_level_dns(data.get("imdata"))

    def post(self, target: str, body: str | Any, *, kind: query.Kind, dry_run: bool = False) -> Any:
        """POST a JSON body to a class or an MO.

        ``kind`` says what ``target`` is, as on :meth:`get`. ``body`` is JSON
        text, or an object to serialize. Text is sent exactly as given. With
        ``dry_run``, nothing is sent and :meth:`dry_run` runs instead -- the
        return value is then a list of :class:`~a4i.mo.Change`.

        ``node`` has no counterpart here on purpose: a switch's MIT is a
        projection of the policy the APIC resolved onto it, so configuration
        written there is overwritten on the next policy resolution.
        """

        if dry_run:
            return self.dry_run(target, body, kind=kind)
        text, _ = read_body(body)
        return self._transport.post(target, kind, text)

    def dry_run(self, target: str, body: str | Any, *, kind: query.Kind) -> list[mo.Change]:
        """Return the changes posting ``body`` would cause, reading the fabric first.

        The APIC has no server-side dry run, so the current state is fetched and
        the comparison happens here, in :func:`a4i.dry_run.check`. An empty list
        means the POST would change nothing at all.

        What is fetched is the subtree of each MO the body stands at, one
        request per subtree and each of them checked against its own
        ``totalCount``: a response that came back paged would otherwise read as
        a fabric missing everything past the page, and every MO on the far side
        of it would be reported as one this POST creates. A body wrapped in
        ``polUni`` is fetched one top-level subtree at a time rather than as uni
        whole, for the reason :meth:`_fetch_uni` gives.

        A caller already holding a fabric -- :meth:`fetch` returns one -- calls
        :func:`a4i.dry_run.check` with it instead, and sends nothing. That is
        what ``post --dry-run`` does when ``a4i fetch`` has left one in the
        daemon; this is what it falls back to when none has.

        The body is refused before any GET goes out on the strength of it: the
        placement is worked out first, and only the DNs it arrives at are read.
        """

        _, parsed = read_body(body)
        intended = merge.read(dry_run.rooted(target, kind, parsed))
        current = self._fetch_subtrees(dry_run.roots(intended.index))
        return dry_run.check(target, parsed, kind=kind, fabric=current)

    def fetch(self) -> dict[str, Any]:
        """Return the fabric's own configuration, shaped as :func:`a4i.merge.merge` shapes one.

        Everything under uni is read one top-level subtree at a time, and what
        comes back is folded into the one body those MOs describe: a ``polUni``
        holding each of them nested under the MO it hangs off, siblings in RN
        order. Nothing is compared -- this is the fabric written as an intended
        configuration, ready to be kept in git, posted at uni, or handed to
        :func:`a4i.diff.compare` and :func:`a4i.plan.create` as the fabric they
        compare against.

        What the APIC sends is every settable property, its defaults included,
        so this is a great deal longer than the configuration a person would
        have written for the same fabric.

        Raises :class:`ValueError` if the fabric holds an MO that no single body
        posted at uni could carry -- see :func:`a4i.merge.merge`. That is the
        bundled dictionary disagreeing with the fabric in front of it, and it
        names the MO rather than quietly leaving it out.
        """

        imdata = self._fetch_uni()
        if not imdata:
            return merge.empty()
        return merge.merge(imdata)

    # -- internals --------------------------------------------------------

    def _fetch_uni(self) -> list[Any]:
        """Fetch every MO under uni, one top-level subtree per request.

        One ``rsp-subtree=full`` over uni is what a large fabric times out on,
        and splitting at the top level names the subtree that failed. A subtree
        that cannot be read is fatal rather than skipped: a difference missed in
        what is gone would read as a fabric that matches.
        """

        return self._fetch_subtrees(self._top_level_dns())

    def _fetch_subtrees(self, dns: Sequence[str]) -> list[Any]:
        """Fetch one subtree per DN and concatenate what comes back."""

        imdata: list[Any] = []
        for dn in dns:
            data = self._fetch(dn, dict(_CURRENT_STATE))
            imdata.extend(data.get("imdata") or [])
        return imdata

    def _top_level_dns(self) -> list[str]:
        """Return the DNs of the MOs hanging directly under uni."""

        data = self._fetch(merge.ROOT, dict(_UNI_CHILDREN))
        return mo.top_level_dns(data.get("imdata"))

    def _fetch(self, dn: str, params: dict[str, str], *, kind: query.Kind = "mo") -> Any:
        """GET one subtree, saying which one if the APIC refuses it.

        A short response is refused too: the APIC pages a long one rather than
        failing, and a comparison cannot tell a page from the whole.
        """

        try:
            data = self._transport.get(dn, kind, params, None)
        except ApicError as exc:
            raise ApicError(f"{dn}: {exc}", code=exc.code, status=exc.status) from None
        _check_complete(dn, data)
        return data

    def _own_session(self) -> Session:
        if self._session is None:
            raise TypeError(_NO_SESSION)
        return self._session


class AsyncClient:
    """:class:`Client`, awaited.

    The same arguments, the same return values and the same exceptions, sending
    the same requests in the same order. Every method below is documented on
    :class:`Client`, which is the one description of what a call means.

    ``transport`` replaces the session this client would otherwise build, as on
    :class:`Client`, and must be an awaited one. There is no awaited daemon
    transport, so a client here always holds a session of its own.
    """

    def __init__(
        self,
        host: str | None = None,
        *,
        verify: bool | str = True,
        timeout: float | None = None,
        transport: AsyncTransport | None = None,
    ) -> None:
        if transport is None:
            if host is None:
                raise TypeError("a host is required")
            # Imported here rather than at module scope, for the reason
            # Client.__init__ gives.
            from a4i.session import DEFAULT_TIMEOUT, AsyncSession

            transport = AsyncDirectTransport(
                AsyncSession(
                    host,
                    verify=verify,
                    timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
                )
            )
        self._transport = transport
        self._session: AsyncSession | None = getattr(transport, "session", None)

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # -- authentication ---------------------------------------------------

    async def login(self, user: str, password: str) -> None:
        """Authenticate to the APIC and hold the token in memory."""

        await self._own_session().login(user, password)

    async def logout(self) -> None:
        """End the session on the APIC and drop the token. See :meth:`Client.logout`."""

        await self._own_session().logout()

    async def refresh(self) -> None:
        """Refresh the token now. See :meth:`Client.refresh`."""

        await self._own_session().refresh()

    async def close(self) -> None:
        """Close the connections. A transport holding no session of its own has none."""

        if self._session is not None:
            await self._session.close()

    @property
    def logged_in(self) -> bool:
        return self._session is not None and self._session.logged_in

    # -- requests ---------------------------------------------------------

    async def get(
        self,
        target: str,
        *,
        kind: query.Kind,
        query_target: str | None = None,
        target_subtree_class: str | Sequence[str] | None = None,
        query_target_filter: str | None = None,
        rsp_subtree: str | None = None,
        rsp_subtree_class: str | Sequence[str] | None = None,
        rsp_subtree_filter: str | None = None,
        rsp_subtree_include: str | Sequence[str] | None = None,
        rsp_prop_include: str | None = None,
        order_by: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        node: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """GET a class or an MO, and return the APIC's response as it came.

        The arguments are :meth:`Client.get`'s, down to the values they refuse.
        """

        built = query.build_get_params(
            query_target=query_target,
            target_subtree_class=target_subtree_class,
            query_target_filter=query_target_filter,
            rsp_subtree=rsp_subtree,
            rsp_subtree_class=rsp_subtree_class,
            rsp_subtree_filter=rsp_subtree_filter,
            rsp_subtree_include=rsp_subtree_include,
            rsp_prop_include=rsp_prop_include,
            order_by=order_by,
            page=page,
            page_size=page_size,
            params=params,
        )
        return await self._transport.get(target, kind, built, node)

    async def list_children(self, dn: str, *, node: str | None = None) -> list[str]:
        """Return the DNs of the MOs directly under ``dn``. See :meth:`Client.list_children`."""

        data = await self._transport.get(dn, "mo", dict(_CHILDREN), node)
        return mo.top_level_dns(data.get("imdata"))

    async def post(
        self, target: str, body: str | Any, *, kind: query.Kind, dry_run: bool = False
    ) -> Any:
        """POST a JSON body to a class or an MO. See :meth:`Client.post`."""

        if dry_run:
            return await self.dry_run(target, body, kind=kind)
        text, _ = read_body(body)
        return await self._transport.post(target, kind, text)

    async def dry_run(self, target: str, body: str | Any, *, kind: query.Kind) -> list[mo.Change]:
        """Return the changes posting ``body`` would cause, sending nothing.

        See :meth:`Client.dry_run`.
        """

        _, parsed = read_body(body)
        intended = merge.read(dry_run.rooted(target, kind, parsed))
        current = await self._fetch_subtrees(dry_run.roots(intended.index))
        return dry_run.check(target, parsed, kind=kind, fabric=current)

    async def fetch(self) -> dict[str, Any]:
        """Return the fabric's own configuration as one body. See :meth:`Client.fetch`."""

        imdata = await self._fetch_uni()
        if not imdata:
            return merge.empty()
        return merge.merge(imdata)

    # -- internals --------------------------------------------------------

    async def _fetch_uni(self) -> list[Any]:
        """Fetch every MO under uni, one top-level subtree per request.

        One request at a time, as :meth:`Client._fetch_uni` makes them: the load
        a fabric sees is then the load the CLI puts on it, whoever is asking.
        """

        return await self._fetch_subtrees(await self._top_level_dns())

    async def _fetch_subtrees(self, dns: Sequence[str]) -> list[Any]:
        """Fetch one subtree per DN and concatenate what comes back."""

        imdata: list[Any] = []
        for dn in dns:
            data = await self._fetch(dn, dict(_CURRENT_STATE))
            imdata.extend(data.get("imdata") or [])
        return imdata

    async def _top_level_dns(self) -> list[str]:
        """Return the DNs of the MOs hanging directly under uni."""

        data = await self._fetch(merge.ROOT, dict(_UNI_CHILDREN))
        return mo.top_level_dns(data.get("imdata"))

    async def _fetch(self, dn: str, params: dict[str, str], *, kind: query.Kind = "mo") -> Any:
        """GET one subtree, saying which one if the APIC refuses it.

        See :meth:`Client._fetch` for why a short response is refused too.
        """

        try:
            data = await self._transport.get(dn, kind, params, None)
        except ApicError as exc:
            raise ApicError(f"{dn}: {exc}", code=exc.code, status=exc.status) from None
        _check_complete(dn, data)
        return data

    def _own_session(self) -> AsyncSession:
        if self._session is None:
            raise TypeError(_NO_SESSION)
        return self._session


def _check_complete(dn: str, data: Any) -> None:
    """Raise when the APIC returned fewer MOs than it says the query has.

    ``totalCount`` counts what the query matched, so for a subtree GET it counts
    the MO at its root alone: this catches a truncated list of children and not a
    truncated subtree. A response carrying no count at all is left alone.
    """

    if not isinstance(data, Mapping):
        return
    try:
        total = int(data.get("totalCount"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return
    imdata = data.get("imdata")
    returned = len(imdata) if isinstance(imdata, list) else 0
    if returned < total:
        raise ApicError(
            f"{dn}: the APIC returned {returned} of {total} MOs, so this is one page of "
            f"the answer and not all of it"
        )
