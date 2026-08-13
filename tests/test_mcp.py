"""The MCP server, from the protocol down to the fabric.

The JSON-RPC layer is written out in a4i rather than taken from an SDK, so it is
tested the way a client exercises it: by handing :class:`~a4i.mcp.server.Server`
whole messages and reading whole replies. Underneath, the tools run against the
same mocked APIC every other test uses, so one test can follow a call from the
model's request through the daemon to the fabric and back.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket as socket_mod
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from a4i import ipc
from a4i.daemon import Daemon
from a4i.errors import DaemonError
from a4i.mcp import guides, tools
from a4i.mcp.server import LATEST_VERSION, RESOURCE_PREFIX, Server, serve
from apic_mock import Clock, make_session_factory


def _wait_for_socket(path) -> None:
    for _ in range(200):
        sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        try:
            sock.connect(str(path))
            sock.close()
            return
        except OSError:
            time.sleep(0.01)
    raise RuntimeError("daemon did not start")


@pytest.fixture
def daemon(monkeypatch):
    """A real daemon on a private socket, serving the mocked APIC."""

    state: dict = {}
    clock = Clock()
    sock_dir = Path(tempfile.gettempdir()) / f"a4i-m-{uuid.uuid4().hex[:8]}"
    sock_path = sock_dir / "daemon.sock"
    server = Daemon(str(sock_path), clock=clock, session_factory=make_session_factory(state, clock))
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    _wait_for_socket(sock_path)
    monkeypatch.setattr(ipc, "socket_path", lambda: sock_path)
    yield state
    with contextlib.suppress(DaemonError):
        ipc.stop()
    thread.join(timeout=2)
    shutil.rmtree(sock_dir, ignore_errors=True)


@pytest.fixture
def no_daemon(monkeypatch):
    """A socket path nothing is listening on, as before anyone has logged in.

    Under the system temp dir rather than pytest's, whose path is long enough to
    overflow an AF_UNIX address and turn "nothing is listening" into "this socket
    cannot be used" -- a different case with a different answer.
    """

    directory = Path(tempfile.gettempdir()) / f"a4i-n-{uuid.uuid4().hex[:8]}"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(ipc, "socket_path", lambda: directory / "daemon.sock")
    yield
    shutil.rmtree(directory, ignore_errors=True)


def _login(read_only: bool = False) -> ipc.LoginReply:
    return ipc.login("apic.test", "admin", "pw", verify=False, read_only=read_only)


def _request(method: str, params: dict | None = None, message_id: int = 1) -> dict:
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _call(server: Server, method: str, params: dict | None = None) -> dict:
    replies = server.handle(_request(method, params))
    assert replies, f"{method} produced no reply"
    return replies[0]


def _tool_text(server: Server, name: str, arguments: dict) -> tuple[str, bool]:
    reply = _call(server, "tools/call", {"name": name, "arguments": arguments})
    result = reply["result"]
    return result["content"][0]["text"], result["isError"]


# -- protocol --------------------------------------------------------------


def test_initialize_answers_in_the_clients_own_version(no_daemon) -> None:
    reply = _call(Server(), "initialize", {"protocolVersion": "2024-11-05"})
    result = reply["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"]["tools"]["listChanged"] is True
    assert result["serverInfo"]["name"] == "a4i"


def test_initialize_falls_back_to_the_newest_known_version(no_daemon) -> None:
    reply = _call(Server(), "initialize", {"protocolVersion": "1999-01-01"})
    assert reply["result"]["protocolVersion"] == LATEST_VERSION


def test_initialize_carries_the_instructions(no_daemon) -> None:
    reply = _call(Server(), "initialize", {})
    instructions = reply["result"]["instructions"]
    # The two things a client that never reads a resource still has to know.
    assert "dry_run" in instructions
    assert "read-only" in instructions


def test_notification_gets_no_reply(no_daemon) -> None:
    assert Server().handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_unknown_method_is_a_json_rpc_error(no_daemon) -> None:
    reply = _call(Server(), "resources/subscribe")
    assert reply["error"]["code"] == -32601
    assert "resources/subscribe" in reply["error"]["message"]


def test_a_message_that_is_not_an_object_is_refused(no_daemon) -> None:
    (reply,) = Server().handle(["not", "a", "message"])
    assert reply["error"]["code"] == -32600


def test_ping_is_answered_empty(no_daemon) -> None:
    assert _call(Server(), "ping")["result"] == {}


def test_unparseable_line_is_answered_with_a_parse_error(no_daemon) -> None:
    import io

    out = io.StringIO()
    serve(io.StringIO("{not json\n"), out)
    assert json.loads(out.getvalue())["error"]["code"] == -32700


def test_serve_reads_a_stream_of_messages(no_daemon) -> None:
    import io

    lines = "\n".join(
        json.dumps(_request(method, message_id=n))
        for n, method in enumerate(["initialize", "ping", "resources/list"], start=1)
    )
    out = io.StringIO()
    serve(io.StringIO(lines + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3]


# -- resources -------------------------------------------------------------


def test_every_guide_is_listed_and_readable(no_daemon) -> None:
    server = Server()
    listed = _call(server, "resources/list")["result"]["resources"]
    assert {resource["uri"] for resource in listed} == {
        f"{RESOURCE_PREFIX}{name}" for name in guides.GUIDES
    }
    for resource in listed:
        contents = _call(server, "resources/read", {"uri": resource["uri"]})["result"]["contents"]
        assert contents[0]["uri"] == resource["uri"]
        assert contents[0]["text"].strip()


def test_an_unknown_resource_is_refused(no_daemon) -> None:
    reply = _call(Server(), "resources/read", {"uri": "a4i://guide/nonesuch"})
    assert reply["error"]["code"] == -32602


# -- the tool list follows the daemon --------------------------------------


def test_post_is_offered_when_nobody_has_logged_in(no_daemon) -> None:
    """A client lists tools before the user logs in, so read-only is not yet known.

    Hiding post on the strength of a session that does not exist would hide it
    from the ordinary case too: a client that lists once at startup would never
    see it again.
    """

    names = [tool["name"] for tool in _call(Server(), "tools/list")["result"]["tools"]]
    assert "post" in names


def test_post_is_offered_on_a_writable_session(daemon) -> None:
    _login()
    names = [tool["name"] for tool in _call(Server(), "tools/list")["result"]["tools"]]
    assert "post" in names


def test_post_is_withheld_on_a_read_only_session(daemon) -> None:
    _login(read_only=True)
    names = [tool["name"] for tool in _call(Server(), "tools/list")["result"]["tools"]]
    assert "post" not in names
    # Everything that only reads stays, dry_run included: it is the whole point
    # of a read-only session that a model can still work out what a change means.
    assert {"get", "dry_run", "diff", "list", "describe", "search"} <= set(names)


def test_becoming_read_only_notifies_the_client(daemon) -> None:
    server = Server()
    _login()
    assert "post" in [tool["name"] for tool in _call(server, "tools/list")["result"]["tools"]]

    # The user logs in again, read-only, while the client holds the old list.
    _login(read_only=True)
    replies = server.handle(_request("ping", message_id=2))
    methods = [reply.get("method") for reply in replies]
    assert "notifications/tools/list_changed" in methods

    names = [tool["name"] for tool in _call(server, "tools/list")["result"]["tools"]]
    assert "post" not in names


def test_the_notification_is_sent_once(daemon) -> None:
    server = Server()
    _login()
    _call(server, "tools/list")
    _login(read_only=True)
    assert any(
        reply.get("method") == "notifications/tools/list_changed"
        for reply in server.handle(_request("ping", message_id=2))
    )
    # Nothing changed since, so nothing more is announced.
    assert all(
        reply.get("method") != "notifications/tools/list_changed"
        for reply in server.handle(_request("ping", message_id=3))
    )


# -- tools against the fabric ----------------------------------------------


def test_get_returns_the_apic_response(daemon) -> None:
    _login()
    text, is_error = _tool_text(Server(), "get", {"kind": "class", "target": "fvTenant"})
    assert not is_error
    assert json.loads(text)["imdata"][0]["fvTenant"]["attributes"]["name"] == "common"


def test_get_passes_the_query_parameters_through(daemon) -> None:
    _login()
    _tool_text(
        Server(),
        "get",
        {
            "kind": "class",
            "target": "fvTenant",
            "query_target": "subtree",
            "rsp_prop_include": "config-only",
            "page": 0,
            "page_size": 10,
        },
    )
    assert daemon["last_params"] == {
        "query-target": "subtree",
        "rsp-prop-include": "config-only",
        "page": "0",
        "page-size": "10",
    }


def test_get_reaches_a_switch_with_node(daemon) -> None:
    _login()
    text, is_error = _tool_text(
        Server(), "get", {"kind": "class", "target": "l1PhysIf", "node": "leaf101"}
    )
    assert not is_error
    assert daemon["node_requests"][-1][0] == "leaf101"


def test_a_value_aci_does_not_define_is_the_models_to_fix(daemon) -> None:
    _login()
    text, is_error = _tool_text(
        Server(), "get", {"kind": "class", "target": "fvTenant", "rsp_subtree": "everything"}
    )
    assert is_error
    assert "rsp_subtree" in text


def test_an_apic_failure_is_reported_as_a_tool_error(daemon) -> None:
    _login()
    text, is_error = _tool_text(Server(), "get", {"kind": "class", "target": "boom"})
    assert is_error
    assert "boom failed" in text


def test_a_response_over_the_limit_is_refused_with_a_way_out(daemon, monkeypatch) -> None:
    _login()
    monkeypatch.setenv(tools.MAX_BYTES_VAR, "10")
    text, is_error = _tool_text(Server(), "get", {"kind": "class", "target": "fvTenant"})
    assert is_error
    assert "Response too large" in text
    # The refusal has to say what would make it smaller, or a retry is blind.
    assert "config-only" in text
    assert "page_size" in text


def test_without_a_session_the_tool_names_the_command_to_run(no_daemon) -> None:
    text, is_error = _tool_text(Server(), "get", {"kind": "class", "target": "fvTenant"})
    assert is_error
    assert "a4i login" in text


def test_list_mo_returns_child_dns(daemon) -> None:
    # What the query asks for is Client.list_children's and is verified there;
    # what this shows is that the tool goes through it and hands the DNs over
    # one per line, which is the same answer the command prints.
    _login()
    text, is_error = _tool_text(Server(), "list", {"kind": "mo", "dn": "uni"})
    assert not is_error
    assert text.splitlines() == ["uni/epp", "uni/tn-common", "uni/tn-infra"]


def test_list_class_needs_no_session(no_daemon) -> None:
    text, is_error = _tool_text(Server(), "list", {"kind": "class", "prefix": "fvBD"})
    assert not is_error
    assert "fvBD" in text.splitlines()


def test_post_writes_the_body(daemon) -> None:
    _login()
    body = {"fvTenant": {"attributes": {"name": "demo"}}}
    text, is_error = _tool_text(
        Server(), "post", {"kind": "mo", "target": "uni/tn-demo", "body": body}
    )
    assert not is_error
    assert daemon["last_method"] == "POST"
    assert json.loads(daemon["last_body"]) == body


def test_post_is_refused_on_a_read_only_session(daemon) -> None:
    """The daemon refuses it even though the tool was never offered.

    Withholding the tool is guidance for the model; the refusal is the guarantee,
    and it does not depend on the model having been told.
    """

    _login(read_only=True)
    text, is_error = _tool_text(
        Server(),
        "post",
        {"kind": "mo", "target": "uni/tn-demo", "body": {"fvTenant": {"attributes": {}}}},
    )
    assert is_error
    assert "read-only" in text
    assert "daemon stop" in text
    assert daemon.get("last_method") != "POST"


def test_dry_run_sends_nothing_and_reports_the_change(daemon) -> None:
    _login(read_only=True)
    text, is_error = _tool_text(
        Server(),
        "dry_run",
        {
            "kind": "mo",
            "target": "uni/tn-infra",
            "body": {"fvTenant": {"attributes": {"name": "infra", "descr": "changed"}}},
        },
    )
    assert not is_error
    assert "descr" in text
    assert daemon.get("last_method") == "GET"


INFRA = {"fvTenant": {"attributes": {"dn": "uni/tn-infra", "name": "infra"}}}


def test_diff_takes_one_inline_body(daemon) -> None:
    _login()
    text, is_error = _tool_text(Server(), "diff", {"body": INFRA, "exclude": ["uni/tn-common"]})
    assert not is_error
    assert "uni/tn-common" not in text


def test_diff_reads_the_body_from_a_path(daemon, tmp_path) -> None:
    _login()
    config = tmp_path / "fabric.json"
    config.write_text(json.dumps(INFRA))
    text, is_error = _tool_text(
        Server(), "diff", {"path": str(config), "exclude": ["uni/tn-common"]}
    )
    assert not is_error
    assert "uni/tn-common" not in text


def test_diff_wants_a_body_or_a_path_and_not_both(daemon, tmp_path) -> None:
    _login()
    for arguments in ({}, {"body": INFRA, "path": str(tmp_path / "x.json")}):
        text, is_error = _tool_text(Server(), "diff", arguments)
        assert is_error
        assert "body" in text and "path" in text


def test_diff_sends_a_directory_back_to_merge(daemon, tmp_path) -> None:
    # A directory here would be diff reading several configurations again, which
    # is the thing merge was split out to own.
    _login()
    text, is_error = _tool_text(Server(), "diff", {"path": str(tmp_path)})
    assert is_error
    assert "merge" in text


def test_diff_reports_a_path_that_is_not_there(daemon, tmp_path) -> None:
    _login()
    text, is_error = _tool_text(Server(), "diff", {"path": str(tmp_path / "gone.json")})
    assert is_error
    assert "gone.json" in text


# -- merge -----------------------------------------------------------------


def test_merge_returns_the_body_it_folded(no_daemon, tmp_path) -> None:
    # It reaches no fabric at all, so it answers without a session.
    config = tmp_path / "fabric.json"
    config.write_text(json.dumps({"fvTenant": {"attributes": {"name": "infra"}}}))
    text, is_error = _tool_text(Server(), "merge", {"paths": [str(config)]})
    assert not is_error
    body = json.loads(text)
    assert body["polUni"]["attributes"] == {"dn": "uni"}
    assert body["polUni"]["children"][0]["fvTenant"]["attributes"]["dn"] == "uni/tn-infra"


def test_merge_lays_inline_bodies_over_what_the_files_say(no_daemon, tmp_path) -> None:
    config = tmp_path / "fabric.json"
    config.write_text(json.dumps({"fvTenant": {"attributes": {"name": "infra", "descr": "old"}}}))
    text, _ = _tool_text(
        Server(),
        "merge",
        {
            "paths": [str(config)],
            "configs": [{"fvTenant": {"attributes": {"name": "infra", "descr": "new"}}}],
        },
    )
    (child,) = json.loads(text)["polUni"]["children"]
    assert child["fvTenant"]["attributes"]["descr"] == "new"


def test_merge_writes_to_a_file_and_keeps_the_body_out_of_the_answer(no_daemon, tmp_path) -> None:
    out = tmp_path / "merged.json"
    text, is_error = _tool_text(
        Server(),
        "merge",
        {"configs": [{"fvTenant": {"attributes": {"name": "infra"}}}], "output": str(out)},
    )
    assert not is_error
    assert "merged 1 MOs" in text and str(out) in text
    assert json.loads(out.read_text())["polUni"]["children"]


def test_merge_refuses_to_replace_a_file_unless_told_to(no_daemon, tmp_path) -> None:
    out = tmp_path / "merged.json"
    out.write_text("keep me")
    arguments = {"configs": [{"fvTenant": {"attributes": {"name": "infra"}}}], "output": str(out)}
    text, is_error = _tool_text(Server(), "merge", arguments)
    assert is_error
    assert "already exists" in text
    # The rule is a4i.config's; naming this tool's own argument is what is left
    # for the tool to do, and it is what tells the model how to go on.
    assert "overwrite: true" in text
    assert out.read_text() == "keep me"

    _, is_error = _tool_text(Server(), "merge", {**arguments, "overwrite": True})
    assert not is_error
    assert out.read_text() != "keep me"


def test_merge_with_nothing_to_merge_says_what_it_wants(no_daemon) -> None:
    text, is_error = _tool_text(Server(), "merge", {})
    assert is_error
    assert "paths" in text and "configs" in text


def test_merge_is_offered_to_a_read_only_session() -> None:
    # It writes a local file at most, never the fabric.
    names = [tool["name"] for tool in tools.tool_definitions(read_only=True)]
    assert "merge" in names
    assert "post" not in names


# -- the bundled model -----------------------------------------------------


def test_describe_carries_what_a_body_needs(no_daemon) -> None:
    text, is_error = _tool_text(Server(), "describe", {"class_name": "fvBD"})
    assert not is_error
    record = json.loads(text)
    assert record["class"] == "fvBD"
    assert record["configurable"] is True
    # How its DN is built, what contains it, and a property with its permitted
    # values: between them, enough to write a body without a round trip.
    assert record["rn"] == "BD-{name}"
    assert record["naming"] == ["name"]
    assert "fvTenant" in record["parents"]
    assert record["props"]["name"]["naming"] is True
    assert set(record["props"]["arpFlood"]["values"]) == {"no", "yes"}


def test_describe_lists_only_configurable_children(no_daemon) -> None:
    """The children a body could actually write, not everything the fabric hangs there.

    The source lists 317 classes under fvBD, almost all of them counters the
    fabric maintains for itself. Carrying those would be ten times the dictionary
    spent on a question no body asks.
    """

    record = json.loads(_tool_text(Server(), "describe", {"class_name": "fvBD"})[0])
    assert "fvSubnet" in record["children"] and "fvRsCtx" in record["children"]
    assert len(record["children"]) < 50
    # Statistics counters hang under a BD but nothing puts them there.
    assert not [name for name in record["children"] if "FltCounter" in name]


def test_describe_marks_what_cannot_be_set(no_daemon) -> None:
    record = json.loads(_tool_text(Server(), "describe", {"class_name": "fvBD"})[0])
    assert record["props"]["mtu"]["readOnly"] is True
    assert "readOnly" not in record["props"]["arpFlood"]


def test_describe_of_an_unknown_class_suggests_and_does_not_pretend(no_daemon) -> None:
    text, is_error = _tool_text(Server(), "describe", {"class_name": "fvbd"})
    assert is_error
    assert "case-sensitive" in text


def test_search_finds_a_class_by_what_it_is_called(no_daemon) -> None:
    text, is_error = _tool_text(Server(), "search", {"keyword": "bridge domain"})
    assert not is_error
    assert any(line.startswith("fvBD\t") for line in text.splitlines())


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("bridge domain", "fvBD"),
        ("contract", "vzBrCP"),
        ("vrf", "fvCtx"),
        ("epg", "fvAEPg"),
        ("subnet", "fvSubnet"),
        ("tenant", "fvTenant"),
    ],
)
def test_search_puts_the_object_above_the_wiring(no_daemon, keyword, expected) -> None:
    """The thing itself, not the relations pointing at it.

    Dozens of classes are labelled "Bridge Domain" -- every relation to one, and
    the abstract policy behind it. A model that asks for three results and gets
    fhsRtBDToFhs, dhcpRtBDToRelayP and infraRsInfraBD has been answered wrongly,
    however defensible each match is on its own.
    """

    text, _ = _tool_text(Server(), "search", {"keyword": keyword, "limit": 3})
    assert text.splitlines()[0].split("\t")[0] == expected


def test_search_honours_its_limit(no_daemon) -> None:
    text, _ = _tool_text(Server(), "search", {"keyword": "policy", "limit": 3})
    assert len(text.splitlines()) == 3


def test_search_that_matches_nothing_says_so(no_daemon) -> None:
    text, is_error = _tool_text(Server(), "search", {"keyword": "zzzznotathing"})
    assert not is_error
    assert "no classes match" in text


# -- schemas ---------------------------------------------------------------


def test_every_tool_declares_a_schema(no_daemon) -> None:
    for tool in tools.ALL_TOOLS:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert tool["description"]
        for name in schema["required"]:
            assert name in schema["properties"], f"{tool['name']}: {name} required but undeclared"


def _cli_get_options() -> set[str]:
    """Return the option names 'a4i get class' takes, as the tool would name them."""

    import argparse

    from a4i import cli

    parser = argparse.ArgumentParser()
    cli._add_query_options(parser)
    return {
        action.dest
        for action in parser._actions
        # --raw is how a terminal is told not to colour its output, which is not
        # a question an MCP client has.
        if action.dest not in {"help", "raw"}
    }


def test_get_takes_every_query_option_the_cli_does(no_daemon) -> None:
    """The tool's arguments are the CLI's options, which are ACI's parameter names.

    Keeping the two sets equal is what lets a model write a query straight from
    the APIC documentation. A divergence here means a4i has grown a dialect.
    """

    declared = set(tools.GET["inputSchema"]["properties"]) - {"kind", "target"}
    assert declared == _cli_get_options()
