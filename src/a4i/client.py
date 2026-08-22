"""The ACI REST API as a Python object.

Everything a get or a post means lives here: the option-to-parameter mapping,
the validation, and the dry-run comparison. How the request travels is the
:mod:`a4i.transport` layer's business, so the CLI and a library caller run the
very same code and send the very same request.

A client made from a host owns its session and authenticates with
:meth:`Client.login`; the token stays in this process's memory for as long as
the client lives. No daemon is involved, and nothing is written to disk.

:class:`AsyncClient` is the same object awaited. It is spelled out separately
rather than shared with :class:`Client`, because what the two have in common is
already outside both of them -- the parameters in :mod:`a4i.query`, the
comparisons in :mod:`a4i.dry_run` and :mod:`a4i.diff` -- and what is left is
the awaiting.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any

from a4i import diff, dry_run, merge, mo, query, validate
from a4i.errors import ApicError
from a4i.transport import AsyncDirectTransport, AsyncTransport, DirectTransport, Transport

if TYPE_CHECKING:
    from a4i.session import AsyncSession, Session

# What a dry run needs to see of the current tree: the whole subtree, because
# the body may reach into it, and only the settable properties, because only
# those can change. A fabric-wide comparison wants exactly the same of each
# subtree it walks.
_CURRENT_STATE = {"rsp-subtree": "full", "rsp-prop-include": "config-only"}

# Listing what hangs under uni needs the DNs and nothing else. "config-only"
# rather than "naming-only" because this list decides what gets walked, and
# walking it with "config-only" below would find nothing in a container that
# holds no configuration -- uni's runtime children, which nobody wrote and which
# no intended configuration will mention. Listing them only to report them extra
# is the wrong answer twice. It returns the DNs all the same.
_UNI_CHILDREN = {"query-target": "children", "rsp-prop-include": "config-only"}

# What listing the children of a DN asks for: the least the APIC can send while
# still naming each child. Deliberately not _UNI_CHILDREN, which narrows to what
# carries configuration because it decides what a comparison walks; this is a
# browse, and leaves nothing out.
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
        text, _ = _read_body(body)
        return self._transport.post(target, kind, text)

    def dry_run(self, target: str, body: str | Any, *, kind: query.Kind) -> list[mo.Change]:
        """Return the changes posting ``body`` would cause, sending nothing.

        The APIC has no server-side dry run, so the current state is fetched and
        the comparison happens here. An empty list means the POST would change
        nothing at all.

        What is fetched is the subtree the body stands at, one request per
        subtree and each of them checked against its own ``totalCount``: a
        response that came back paged would otherwise read as a fabric missing
        everything past the page, and every MO on the far side of it would be
        reported as one this POST creates. A body wrapped in ``polUni`` is
        fetched one top-level subtree at a time rather than as uni whole, for
        the reason :meth:`_fetch_uni` gives.
        """

        _, parsed = _read_body(body)
        # Before the walk below, not during it: the body is refused as a whole,
        # and refused before any GET goes out on the strength of it.
        validate.check(parsed)
        # A body may be a single MO or an array of them, each rooted at its own DN.
        roots = parsed if isinstance(parsed, list) else [parsed]
        changes: list[mo.Change] = []
        for root in roots:
            for subtree in dry_run.subtrees(target, kind, root):
                imdata = None
                if subtree.identified:
                    fetched = self._fetch(subtree.dn, dict(_CURRENT_STATE))
                    imdata = fetched.get("imdata")
                changes.extend(
                    dry_run.compare(subtree.mo, imdata, subtree.dn, identified=subtree.identified)
                )
        return changes

    def diff(
        self,
        config: str | Any,
        *,
        expand: bool = False,
        exclude: str | Sequence[str] | None = None,
    ) -> list[mo.Change]:
        """Return how the fabric differs from the configuration ``config`` gives.

        ``config`` is one ACI body -- one MO, a list of them, or the same as
        JSON text -- describing the configuration the whole of ``uni`` is meant
        to have. It is one body, as :meth:`post` takes one body; several are
        folded into one beforehand with :func:`a4i.merge.merge`. Everything
        under ``uni`` is then fetched and compared both ways, so an MO or an
        attribute the fabric carries and the configuration does not is reported
        as well. An empty list means the fabric matches.

        ``exclude`` is a DN, or a sequence of them, to leave out of the
        comparison along with everything under each: nothing about those MOs is
        reported, whichever side carries them. A single string is one DN and is
        never split, since an ACI naming value can hold a comma. A "*" in one
        makes it a pattern matching within a single RN -- ``uni/tn-test*`` is
        every tenant whose name starts with ``test`` -- and everything else,
        brackets included, matches itself. See :class:`a4i.mo.Exclusions`.

        This writes nothing: it issues GETs and nothing else, an excluded
        subtree included -- the exclusion is a comparison narrowed, not a fabric
        half read. Without ``expand``, a wholly missing or wholly extra subtree
        is reported as its top MO alone, with the MOs below it counted rather
        than listed.
        """

        _, parsed = _read_body(config)
        return diff.compare(parsed, self._fetch_uni(), expand=expand, exclude=exclude)

    # -- internals --------------------------------------------------------

    def _fetch_uni(self) -> list[Any]:
        """Fetch every MO under uni, one top-level subtree per request.

        A single ``rsp-subtree=full`` over uni would be one request, but also
        one response carrying the fabric's entire configuration, which a large
        fabric times out on. Splitting at the top level holds each response to
        one tenant or one policy tree, and names the subtree that failed if one
        does. A subtree that cannot be read is fatal rather than skipped: a
        difference found in what is left would be real, but one missed in what
        is gone would read as a fabric that matches.
        """

        imdata: list[Any] = []
        for dn in self._top_level_dns():
            data = self._fetch(dn, dict(_CURRENT_STATE))
            imdata.extend(data.get("imdata") or [])
        return imdata

    def _top_level_dns(self) -> list[str]:
        """Return the DNs of the MOs hanging directly under uni.

        The DNs are read the way :meth:`list_children` reads them; only the
        query differs, for the reason ``_UNI_CHILDREN`` gives.
        """

        data = self._fetch(merge.ROOT, dict(_UNI_CHILDREN))
        return mo.top_level_dns(data.get("imdata"))

    def _fetch(self, dn: str, params: dict[str, str]) -> Any:
        """GET one subtree, saying which one if the APIC refuses it.

        A short response is refused too. The APIC pages a long one rather than
        failing, and a comparison cannot tell a page from the whole: every MO
        left off the end would read as one the fabric is not carrying. Better to
        stop and say so than to report a fabric missing what it has.
        """

        try:
            data = self._transport.get(dn, "mo", params, None)
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
    the same requests in the same order: only the sending is awaited, so a
    caller keeps its event loop while the APIC thinks. Every method below is
    documented on :class:`Client`, which is the one description of what a call
    means.

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
        text, _ = _read_body(body)
        return await self._transport.post(target, kind, text)

    async def dry_run(self, target: str, body: str | Any, *, kind: query.Kind) -> list[mo.Change]:
        """Return the changes posting ``body`` would cause, sending nothing.

        See :meth:`Client.dry_run`.
        """

        _, parsed = _read_body(body)
        # Before the walk below, not during it: the body is refused as a whole,
        # and refused before any GET goes out on the strength of it.
        validate.check(parsed)
        # A body may be a single MO or an array of them, each rooted at its own DN.
        roots = parsed if isinstance(parsed, list) else [parsed]
        changes: list[mo.Change] = []
        for root in roots:
            for subtree in dry_run.subtrees(target, kind, root):
                imdata = None
                if subtree.identified:
                    fetched = await self._fetch(subtree.dn, dict(_CURRENT_STATE))
                    imdata = fetched.get("imdata")
                changes.extend(
                    dry_run.compare(subtree.mo, imdata, subtree.dn, identified=subtree.identified)
                )
        return changes

    async def diff(
        self,
        config: str | Any,
        *,
        expand: bool = False,
        exclude: str | Sequence[str] | None = None,
    ) -> list[mo.Change]:
        """Return how the fabric differs from the configuration ``config`` gives.

        See :meth:`Client.diff`.
        """

        _, parsed = _read_body(config)
        return diff.compare(parsed, await self._fetch_uni(), expand=expand, exclude=exclude)

    # -- internals --------------------------------------------------------

    async def _fetch_uni(self) -> list[Any]:
        """Fetch every MO under uni, one top-level subtree per request.

        One request at a time, as :meth:`Client._fetch_uni` makes them. The
        subtrees could be fetched at once here, but a comparison that reports
        the same fabric either way is worth more than one that is quicker on the
        way to it, and the load a fabric sees is then the load the CLI puts on
        it.
        """

        imdata: list[Any] = []
        for dn in await self._top_level_dns():
            data = await self._fetch(dn, dict(_CURRENT_STATE))
            imdata.extend(data.get("imdata") or [])
        return imdata

    async def _top_level_dns(self) -> list[str]:
        """Return the DNs of the MOs hanging directly under uni.

        See :meth:`Client._top_level_dns`.
        """

        data = await self._fetch(merge.ROOT, dict(_UNI_CHILDREN))
        return mo.top_level_dns(data.get("imdata"))

    async def _fetch(self, dn: str, params: dict[str, str]) -> Any:
        """GET one subtree, saying which one if the APIC refuses it.

        See :meth:`Client._fetch` for why a short response is refused too.
        """

        try:
            data = await self._transport.get(dn, "mo", params, None)
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

    ``totalCount`` counts what the query matched, which for a subtree GET is the
    one MO at its root: the subtree hangs inside that one and is not counted, so
    this catches a truncated list -- the children of uni -- and not a truncated
    subtree. Anything but a number to compare against is left alone, since a
    response without one is not evidence of a short one.
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


def _read_body(body: str | Any) -> tuple[str, Any]:
    """Return the text to send and the object it parses to.

    Text is passed through untouched rather than reserialized, so the key order
    and the formatting reach the APIC exactly as the caller wrote them. A diff
    reads its one configuration through here too, and wants only the object --
    which is why the message below says "body" rather than "POST body".
    """

    if not isinstance(body, str):
        return json.dumps(body), body
    if not body.strip():
        raise ValueError("empty body")
    try:
        return body, json.loads(body)
    except ValueError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from None
