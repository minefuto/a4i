from __future__ import annotations

import argparse
import io
import json

import pytest

from a4i import cli, ipc
from a4i.errors import (
    DaemonError,
    NoDaemonError,
    NotLoggedInError,
    UnusableSocketError,
)
from a4i.session import DEFAULT_TIMEOUT

# -- an unusable socket must not read as "no daemon" ------------------------
#
# logout, daemon status and daemon stop all treat a failed request as "there is
# no daemon", which would otherwise hide a socket this client refuses to use.


def _raise_session_op(monkeypatch, exc: BaseException) -> None:
    """Make every session op fail the same way, whichever command asks."""

    def fail(*args, **kwargs):
        raise exc

    for op in ("login", "logout", "status", "stop"):
        monkeypatch.setattr(ipc, op, fail)


def _raise_request(monkeypatch, exc: BaseException) -> None:
    """Make the get and post ops fail, for the commands that issue them."""

    def fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr(ipc, "get", fail)
    monkeypatch.setattr(ipc, "post", fail)


def _record(monkeypatch, sent: list[dict], reply) -> None:
    """Stand in for the daemon: record what each op was asked, and answer it.

    ``reply`` is the answer, or a callable taking the target and returning one.
    """

    def get(target, kind, params, node, *, autostart=True):
        sent.append(
            {"op": "get", "target": target, "kind": kind, "params": params or {}, "node": node}
        )
        return reply(target) if callable(reply) else reply

    def post(target, kind, body, *, autostart=True):
        sent.append({"op": "post", "target": target, "kind": kind, "body": body})
        return reply(target) if callable(reply) else reply

    monkeypatch.setattr(ipc, "get", get)
    monkeypatch.setattr(ipc, "post", post)


@pytest.fixture
def args() -> argparse.Namespace:
    return argparse.Namespace()


@pytest.mark.parametrize(
    "command",
    [cli._cmd_logout, cli._cmd_daemon_status, cli._cmd_daemon_stop],
)
def test_socket_error_is_reported(monkeypatch, capsys, args, command) -> None:
    _raise_session_op(monkeypatch, UnusableSocketError("/tmp/a4i-0 must be mode 0700, not 0777"))
    assert command(args) == 1
    captured = capsys.readouterr()
    assert "must be mode 0700" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (cli._cmd_logout, "logged out"),
        (cli._cmd_daemon_status, "daemon not running"),
        (cli._cmd_daemon_stop, "daemon stopped"),
    ],
)
def test_missing_daemon_is_not_an_error(monkeypatch, capsys, args, command, expected) -> None:
    _raise_session_op(monkeypatch, NoDaemonError("no a4i daemon is running"))
    assert command(args) == 0
    assert expected in capsys.readouterr().out


# -- an APIC that could not be told the session ended -----------------------


def _reply_session_op(monkeypatch, data: dict) -> None:
    def reply(*args, **kwargs):
        return data

    for op in ("logout", "stop"):
        monkeypatch.setattr(ipc, op, reply)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (cli._cmd_logout, "logged out"),
        (cli._cmd_daemon_stop, "daemon stopped"),
    ],
)
def test_unnotified_apic_warns_but_succeeds(monkeypatch, capsys, args, command, expected) -> None:
    _reply_session_op(monkeypatch, {"apic_error": "cannot reach https://apic.test"})
    assert command(args) == 0
    captured = capsys.readouterr()
    assert expected in captured.out
    assert "APIC not notified: cannot reach https://apic.test" in captured.err


@pytest.mark.parametrize(
    "command",
    [cli._cmd_logout, cli._cmd_daemon_stop],
)
def test_a_notified_apic_warns_about_nothing(monkeypatch, capsys, args, command) -> None:
    _reply_session_op(monkeypatch, {"apic_error": None})
    assert command(args) == 0
    assert capsys.readouterr().err == ""


# -- login, and what bounds the requests it leaves behind --------------------


def _record_login(monkeypatch) -> list[dict]:
    """Stand in for the daemon's login, recording what it was asked for."""

    sent: list[dict] = []

    def login(host, user, password, *, verify=True, timeout=None, read_only=False):
        sent.append({"host": host, "verify": verify, "timeout": timeout})
        return {
            "host": host,
            "user": user,
            "refresh_timeout": 600.0,
            "timeout": timeout,
            "read_only": read_only,
        }

    monkeypatch.setattr(ipc, "login", login)
    monkeypatch.setenv("APIC_PASSWORD", "pw")
    return sent


def test_login_asks_for_the_timeout_it_was_given(monkeypatch, capsys) -> None:
    sent = _record_login(monkeypatch)
    assert cli.main(["login", "apic.test", "-u", "admin", "--timeout", "120"]) == 0
    assert sent[0]["timeout"] == 120.0


def test_login_without_a_timeout_asks_for_the_session_default(monkeypatch, capsys) -> None:
    # Resolved here rather than left to the daemon so that the number a login
    # asks for is always the number it was given.
    sent = _record_login(monkeypatch)
    assert cli.main(["login", "apic.test", "-u", "admin"]) == 0
    assert sent[0]["timeout"] == DEFAULT_TIMEOUT


def test_the_help_says_the_default_the_session_actually_uses(capsys) -> None:
    """The one place the number is written twice, and what keeps the copy true.

    The parser cannot read DEFAULT_TIMEOUT for itself: reaching it costs httpx2,
    and the parser is built on every tab press.
    """

    with pytest.raises(SystemExit):
        cli.main(["login", "--help"])
    assert f"(default: {DEFAULT_TIMEOUT:g})" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_a_timeout_that_cannot_bound_a_request_is_refused(monkeypatch, capsys, value) -> None:
    # Refused by the parser, before a daemon is started to hear it.
    sent = _record_login(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["login", "apic.test", "-u", "admin", "--timeout", value])
    assert "--timeout" in capsys.readouterr().err
    assert sent == []


def test_daemon_status_tells_the_two_kinds_of_seconds_apart(monkeypatch, capsys, args) -> None:
    """The "expires in" is the token's lifetime; the timeout bounds one request."""

    monkeypatch.setattr(
        ipc,
        "status",
        lambda: {
            "logged_in": True,
            "user": "admin",
            "host": "https://apic.test",
            "read_only": False,
            "expires_in": 540.0,
            "timeout": 120.0,
        },
    )
    assert cli._cmd_daemon_status(args) == 0
    out = capsys.readouterr().out
    assert "expires in 540s" in out
    assert "request timeout 120s" in out


# -- get -------------------------------------------------------------------


def _run_get(monkeypatch, argv: list[str]) -> dict:
    """Parse a get command line and return the kwargs it sends to the daemon."""

    sent: list[dict] = []
    _record(monkeypatch, sent, {"imdata": []})
    assert cli.main(argv) == 0
    return sent[-1]


@pytest.mark.parametrize("node", [None, "leaf101.example.com"])
def test_get_passes_the_node_through(monkeypatch, node) -> None:
    argv = ["get", "class", "fvTenant"] + (["--node", node] if node else [])
    sent = _run_get(monkeypatch, argv)
    assert sent["op"] == "get"
    assert sent["target"] == "fvTenant"
    assert sent["node"] == node


def test_get_sends_no_params_by_default(monkeypatch) -> None:
    # query-target is omitted rather than sent as "self": the APIC default.
    assert _run_get(monkeypatch, ["get", "class", "fvTenant"])["params"] == {}


def test_get_maps_every_option_to_its_aci_parameter(monkeypatch) -> None:
    sent = _run_get(
        monkeypatch,
        [
            "get",
            "class",
            "fvTenant",
            "--query-target",
            "subtree",
            "--target-subtree-class",
            "fvAEPg,fvBD",
            "--query-target-filter",
            'eq(fvTenant.name,"common")',
            "--rsp-subtree",
            "full",
            "--rsp-subtree-class",
            "fvRsPathAtt",
            "--rsp-subtree-filter",
            'gt(fvAEPg.prio,"1")',
            "--rsp-subtree-include",
            "faults,no-scoped",
            "--rsp-prop-include",
            "config-only",
            "--order-by",
            "fvTenant.name|desc",
        ],
    )
    # The option name and the parameter name are the same string throughout.
    assert sent["params"] == {
        "query-target": "subtree",
        "target-subtree-class": "fvAEPg,fvBD",
        "query-target-filter": 'eq(fvTenant.name,"common")',
        "rsp-subtree": "full",
        "rsp-subtree-class": "fvRsPathAtt",
        "rsp-subtree-filter": 'gt(fvAEPg.prio,"1")',
        "rsp-subtree-include": "faults,no-scoped",
        "rsp-prop-include": "config-only",
        "order-by": "fvTenant.name|desc",
    }
    # httpx2 preserves the insertion order, so this is the order the parameters
    # appear in the URL. It follows the APIC documentation's grouping: scoping
    # filters, response subtree filters, then sorting (paging follows, untested
    # here because this command line gives no --page).
    assert list(sent["params"]) == [
        "query-target",
        "target-subtree-class",
        "query-target-filter",
        "rsp-subtree",
        "rsp-subtree-class",
        "rsp-subtree-filter",
        "rsp-subtree-include",
        "rsp-prop-include",
        "order-by",
    ]


def test_get_sends_the_first_page(monkeypatch) -> None:
    # page 0 is a real page, so it must survive a falsy-value filter.
    sent = _run_get(monkeypatch, ["get", "class", "fvTenant", "--page", "0", "--page-size", "100"])
    assert sent["params"] == {"page": "0", "page-size": "100"}


@pytest.mark.parametrize(
    "argv",
    [
        ["get", "class", "fvTenant", "--page", "1"],
        ["get", "class", "fvTenant", "--page-size", "50"],
    ],
)
def test_get_requires_page_and_page_size_together(monkeypatch, capsys, argv) -> None:
    _raise_request(monkeypatch, DaemonError("no a4i daemon is running"))
    assert cli.main(argv) == 1
    assert "page and page_size must be given together" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["get", "class", "fvTenant", "--query-target", "bogus"], "bogus"),
        (["get", "class", "fvTenant", "--rsp-subtree-include", "bogus"], "invalid value: bogus"),
        # Every element of the list is checked, not just the first.
        (
            ["get", "class", "fvTenant", "--rsp-subtree-include", "faults,bogus"],
            "invalid value: bogus",
        ),
        (["get", "class", "fvTenant", "--page", "-1", "--page-size", "10"], "must be 0 or greater"),
        (["get", "class", "fvTenant", "--page", "0", "--page-size", "0"], "must be 1 or greater"),
    ],
)
def test_get_rejects_invalid_values(capsys, argv, message) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main(argv)
    assert exit.value.code == 2
    assert message in capsys.readouterr().err


def test_get_accepts_a_bare_subtree_include_modifier(monkeypatch) -> None:
    sent = _run_get(monkeypatch, ["get", "class", "fvTenant", "--rsp-subtree-include", "count"])
    assert sent["params"] == {"rsp-subtree-include": "count"}


# -- post --dry-run --------------------------------------------------------

TENANT = {
    "imdata": [
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
                "children": [{"fvBD": {"attributes": {"dn": "uni/tn-demo/BD-bd1", "name": "bd1"}}}],
            }
        }
    ]
}


def _run_dry_run(monkeypatch, argv: list[str], response=None) -> tuple[int, list[dict]]:
    """Run a command line and return its exit code and the requests it made."""

    sent: list[dict] = []

    _record(monkeypatch, sent, TENANT if response is None else response)
    return cli.main(argv), sent


def test_dry_run_gets_the_current_subtree_and_never_posts(monkeypatch, capsys) -> None:
    code, sent = _run_dry_run(
        monkeypatch,
        ["post", "mo", "uni/tn-demo", '{"fvTenant":{"attributes":{"descr":"prod"}}}', "--dry-run"],
    )
    assert [request["op"] for request in sent] == ["get"]
    assert sent[0]["target"] == "uni/tn-demo"
    assert sent[0]["params"] == {"rsp-subtree": "full", "rsp-prop-include": "config-only"}
    out = capsys.readouterr().out
    assert "~ fvTenant uni/tn-demo" in out
    assert '~ descr: "" -> "prod"' in out
    assert "0 created, 1 modified, 0 deleted" in out
    # Something would change, so this is not a clean exit.
    assert code == 2


def test_dry_run_reports_no_changes_with_a_clean_exit(monkeypatch, capsys) -> None:
    code, _ = _run_dry_run(
        monkeypatch,
        ["post", "mo", "uni/tn-demo", '{"fvTenant":{"attributes":{"name":"demo"}}}', "--dry-run"],
    )
    assert capsys.readouterr().out.strip() == "no changes"
    assert code == 0


def test_dry_run_needs_a_dn_it_can_work_out(monkeypatch, capsys) -> None:
    code, sent = _run_dry_run(
        monkeypatch,
        ["post", "class", "fvTenant", '{"fvTenant":{"attributes":{"name":"x"}}}', "--dry-run"],
    )
    assert code == 1
    assert sent == []
    assert "cannot determine the target DN" in capsys.readouterr().err


def test_dry_run_queries_each_root_of_an_array_body(monkeypatch, capsys) -> None:
    body = (
        '[{"fvTenant":{"attributes":{"dn":"uni/tn-demo","descr":"prod"}}},'
        ' {"fvTenant":{"attributes":{"dn":"uni/tn-demo","nameAlias":"d"}}}]'
    )
    code, sent = _run_dry_run(monkeypatch, ["post", "mo", "uni", body, "--dry-run"])
    assert [request["target"] for request in sent] == ["uni/tn-demo", "uni/tn-demo"]
    out = capsys.readouterr().out
    assert "0 created, 2 modified, 0 deleted" in out
    assert code == 2


def test_dry_run_is_uncolored_with_raw(monkeypatch, capsys) -> None:
    argv = [
        "post",
        "mo",
        "uni/tn-demo",
        '{"fvTenant":{"attributes":{"descr":"prod"}}}',
        "--dry-run",
    ]
    _run_dry_run(monkeypatch, [*argv, "--raw"])
    assert "\x1b[" not in capsys.readouterr().out


def test_dry_run_reports_a_daemon_error(monkeypatch, capsys) -> None:
    _raise_request(monkeypatch, NotLoggedInError("not logged in"))
    argv = ["post", "mo", "uni/tn-demo", '{"fvTenant":{"attributes":{"descr":"x"}}}', "--dry-run"]
    assert cli.main(argv) == 1
    assert "run 'a4i login'" in capsys.readouterr().err


def test_post_without_dry_run_still_posts(monkeypatch) -> None:
    body = '{"fvTenant":{"attributes":{"name":"demo"}}}'
    code, sent = _run_dry_run(
        monkeypatch, ["post", "mo", "uni/tn-demo", body], response={"imdata": []}
    )
    assert sent == [{"op": "post", "target": "uni/tn-demo", "kind": "mo", "body": body}]
    assert code == 0


def test_post_rejects_an_invalid_body_before_reaching_the_daemon(monkeypatch, capsys) -> None:
    _raise_request(monkeypatch, DaemonError("no a4i daemon is running"))
    assert cli.main(["post", "mo", "uni/tn-demo", "{not json", "--dry-run"]) == 1
    assert "invalid JSON body" in capsys.readouterr().err


# -- diff -------------------------------------------------------------------

# The fabric the mocked daemon serves: uni holds one tenant, fetched whole.
FABRIC = {
    "uni": {"imdata": [{"fvTenant": {"attributes": {"dn": "uni/tn-demo"}}}]},
    "uni/tn-demo": {
        "imdata": [
            {
                "fvTenant": {
                    "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
                    "children": [
                        {
                            "fvBD": {
                                "attributes": {
                                    "dn": "uni/tn-demo/BD-bd1",
                                    "name": "bd1",
                                    "mtu": "1500",
                                }
                            }
                        }
                    ],
                }
            }
        ]
    },
}

# The configuration that describes FABRIC exactly.
INTENDED = {
    "fvTenant": {
        "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        "children": [{"fvBD": {"attributes": {"name": "bd1", "mtu": "1500"}}}],
    }
}


def _run_diff(monkeypatch, argv: list[str]) -> tuple[int, list[dict]]:
    """Run a diff command line and return its exit code and the requests made."""

    sent: list[dict] = []

    _record(monkeypatch, sent, lambda target: FABRIC.get(target, {"imdata": []}))
    return cli.main(argv), sent


def test_diff_lists_uni_then_fetches_each_top_level_subtree(monkeypatch) -> None:
    _, sent = _run_diff(monkeypatch, ["diff", json.dumps(INTENDED)])
    assert [request["op"] for request in sent] == ["get", "get"]
    # Every DN travels bare, with "mo" alongside it saying how to read it.
    assert sent[0]["target"] == "uni"
    assert sent[0]["kind"] == "mo"
    assert sent[0]["params"] == {"query-target": "children", "rsp-prop-include": "config-only"}
    assert sent[1]["target"] == "uni/tn-demo"
    assert sent[1]["params"] == {"rsp-subtree": "full", "rsp-prop-include": "config-only"}


def test_diff_reports_a_clean_fabric_with_a_clean_exit(monkeypatch, capsys) -> None:
    code, _ = _run_diff(monkeypatch, ["diff", json.dumps(INTENDED)])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_takes_the_body_as_an_argument_or_on_stdin(monkeypatch, capsys) -> None:
    # As post does: the argument is the body itself, never a path to one.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(INTENDED)))
    code, _ = _run_diff(monkeypatch, ["diff"])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_reads_a_merged_body_wrapped_in_poluni(monkeypatch, capsys) -> None:
    # What 'a4i merge' writes: a polUni whose children each carry their own dn.
    merged = {
        "polUni": {
            "attributes": {"dn": "uni"},
            "children": [
                {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""}}},
                {
                    "fvBD": {
                        "attributes": {"dn": "uni/tn-demo/BD-bd1", "name": "bd1", "mtu": "1500"}
                    }
                },
            ],
        }
    }
    code, _ = _run_diff(monkeypatch, ["diff", json.dumps(merged)])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_reports_an_attribute_the_fabric_has_and_the_configuration_lacks(
    monkeypatch, capsys
) -> None:
    quiet = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""}}}
    code, _ = _run_diff(monkeypatch, ["diff", json.dumps(quiet)])
    out = capsys.readouterr().out
    # The BD is not mentioned at all, so it is extra; nothing is deleted.
    assert "- fvBD uni/tn-demo/BD-bd1" in out
    assert "1 missing, 0 modified, 1 extra" not in out
    assert "0 missing, 0 modified, 1 extra" in out
    assert code == 2


def test_diff_reports_a_changed_attribute(monkeypatch, capsys) -> None:
    changed = {
        "fvTenant": {
            "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
            "children": [{"fvBD": {"attributes": {"name": "bd1", "mtu": "9000"}}}],
        }
    }
    code, _ = _run_diff(monkeypatch, ["diff", json.dumps(changed)])
    out = capsys.readouterr().out
    assert "~ fvBD uni/tn-demo/BD-bd1" in out
    assert '~ mtu: "1500" -> "9000"' in out
    assert code == 2


def test_diff_says_nothing_about_a_status_the_configuration_carries(monkeypatch, capsys) -> None:
    # A merged body keeps "status" so that a post can still delete. It directs
    # the APIC rather than describing it, so the fabric has nothing to hold it
    # against and the comparison passes over it.
    deleting = {
        "fvTenant": {
            "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "", "status": "deleted"},
            "children": [{"fvBD": {"attributes": {"name": "bd1", "mtu": "1500"}}}],
        }
    }
    code, _ = _run_diff(monkeypatch, ["diff", json.dumps(deleting)])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_expands_a_subtree_on_request(monkeypatch, capsys) -> None:
    other = {"fvTenant": {"attributes": {"dn": "uni/tn-other"}}}
    code, _ = _run_diff(monkeypatch, ["diff", json.dumps(other), "--expand"])
    out = capsys.readouterr().out
    assert "- fvTenant uni/tn-demo" in out
    assert "- fvBD uni/tn-demo/BD-bd1" in out
    assert code == 2


def test_diff_summarises_a_subtree_without_expand(monkeypatch, capsys) -> None:
    other = {"fvTenant": {"attributes": {"dn": "uni/tn-other"}}}
    _run_diff(monkeypatch, ["diff", json.dumps(other)])
    out = capsys.readouterr().out
    assert "- fvTenant uni/tn-demo  (extra: 1 child MO)" in out
    assert "BD-bd1" not in out


def test_diff_leaves_an_excluded_subtree_out_of_the_report(monkeypatch, capsys) -> None:
    # The configuration says nothing about the BD, which would make it extra.
    quiet = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""}}}
    argv = ["diff", json.dumps(quiet), "--exclude", "uni/tn-demo/BD-bd1"]
    code, _ = _run_diff(monkeypatch, argv)
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_takes_exclude_more_than_once(monkeypatch, capsys) -> None:
    # tn-demo would be extra and tn-other missing; excluding both leaves nothing.
    other = {"fvTenant": {"attributes": {"dn": "uni/tn-other"}}}
    argv = ["diff", json.dumps(other), "--exclude", "uni/tn-demo", "--exclude", "uni/tn-other"]
    code, _ = _run_diff(monkeypatch, argv)
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_still_fetches_an_excluded_subtree(monkeypatch) -> None:
    # The exclusion narrows the comparison, not the fetch: the fabric is read the
    # same way whether or not anything is left out of the report.
    argv = ["diff", json.dumps(INTENDED), "--exclude", "uni/tn-demo"]
    _, sent = _run_diff(monkeypatch, argv)
    assert [request["target"] for request in sent] == ["uni", "uni/tn-demo"]


def test_diff_rejects_an_exclude_that_names_no_mo_at_all(monkeypatch, capsys) -> None:
    assert _run_diff(monkeypatch, ["diff", json.dumps(INTENDED), "--exclude", "/"])[0] == 1
    assert "cannot be empty" in capsys.readouterr().err


def test_diff_rejects_a_body_it_cannot_parse(monkeypatch, capsys) -> None:
    assert _run_diff(monkeypatch, ["diff", "{not json"])[0] == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_diff_refuses_a_configuration_that_describes_no_mo(monkeypatch, capsys) -> None:
    # Taken at face value an empty configuration means the whole fabric is
    # extra, and what it means in practice is a path that pointed at nothing.
    code, _ = _run_diff(monkeypatch, ["diff", "[]"])
    captured = capsys.readouterr()
    assert "empty" in captured.err
    assert captured.out == ""
    assert code == 1


def test_diff_refuses_an_mo_it_cannot_tell_from_the_ones_on_the_fabric(monkeypatch, capsys) -> None:
    # The fabric names an fvBD by its "name", and this one gives none.
    unresolvable = {
        "fvTenant": {
            "attributes": {"dn": "uni/tn-demo"},
            "children": [{"fvBD": {"attributes": {"mtu": "9000"}}}],
        }
    }
    code, _ = _run_diff(monkeypatch, ["diff", json.dumps(unresolvable)])
    captured = capsys.readouterr()
    assert "fvBD under uni/tn-demo" in captured.err
    # Nothing is reported: a partial comparison would read as a clean fabric.
    assert captured.out == ""
    assert code == 1


def test_diff_reports_a_daemon_error(monkeypatch, capsys) -> None:
    _raise_request(monkeypatch, NotLoggedInError("not logged in"))
    assert cli.main(["diff", json.dumps(INTENDED)]) == 1
    assert "run 'a4i login'" in capsys.readouterr().err


# -- list -------------------------------------------------------------------


def test_list_class_prints_bundled_names_one_per_line(capsys) -> None:
    # Nothing is sent anywhere: the dictionary ships with a4i.
    assert cli.main(["list", "class", "fvTenant"]) == 0
    names = capsys.readouterr().out.split()
    assert names[0] == "fvTenant"
    assert all(name.startswith("fvTenant") for name in names)


def test_list_class_matches_case_insensitively(capsys) -> None:
    assert cli.main(["list", "class", "FVTEN"]) == 0
    assert "fvTenant" in capsys.readouterr().out.split()


def test_list_class_without_a_prefix_lists_everything(capsys) -> None:
    from a4i.metadata import load_class_names

    assert cli.main(["list", "class"]) == 0
    assert capsys.readouterr().out.split() == list(load_class_names())


def test_list_class_says_nothing_when_nothing_matches(capsys) -> None:
    # An empty list is an answer, not a failure.
    assert cli.main(["list", "class", "zzzz"]) == 0
    assert capsys.readouterr().out == ""


def _run_list_mo(monkeypatch, argv: list[str], response) -> tuple[int, list[dict]]:
    sent: list[dict] = []

    _record(monkeypatch, sent, response)
    return cli.main(argv), sent


CHILDREN = {
    "totalCount": "3",
    "imdata": [
        {"fvTenant": {"attributes": {"dn": "uni/tn-infra"}}},
        {"fvTenant": {"attributes": {"dn": "uni/tn-common"}}},
        {"infraInfra": {"attributes": {"dn": "uni/infra"}}},
    ],
}


# What the query asks for, and that the DNs come back sorted, bare and without
# duplicates, is Client.list_children's and is verified there. What is left here
# is that this command hands its arguments over and prints what comes back.


def test_list_mo_prints_one_dn_per_line(monkeypatch, capsys) -> None:
    code, sent = _run_list_mo(monkeypatch, ["list", "mo", "uni"], CHILDREN)
    assert code == 0
    assert sent[0]["op"] == "get"
    assert sent[0]["target"] == "uni"
    # A line of this is what "a4i get mo" takes as its argument.
    assert capsys.readouterr().out.splitlines() == [
        "uni/infra",
        "uni/tn-common",
        "uni/tn-infra",
    ]


def test_list_mo_defaults_to_uni(monkeypatch) -> None:
    _, sent = _run_list_mo(monkeypatch, ["list", "mo"], CHILDREN)
    assert sent[0]["target"] == "uni"


def test_list_mo_takes_a_dn_with_or_without_a_leading_slash(monkeypatch, capsys) -> None:
    for dn in ("uni/tn-common", "/uni/tn-common"):
        _, sent = _run_list_mo(monkeypatch, ["list", "mo", dn], {"imdata": []})
        assert sent[0]["target"] == dn
    # Either way the daemon builds the same path from kind="mo".
    from a4i import query

    assert query.build_path("/uni/tn-common", "mo") == query.build_path("uni/tn-common", "mo")


def test_list_mo_passes_the_node_through(monkeypatch) -> None:
    _, sent = _run_list_mo(monkeypatch, ["list", "mo", "sys", "--node", "leaf101"], CHILDREN)
    assert sent[0]["node"] == "leaf101"


def test_list_mo_prints_nothing_for_a_leaf(monkeypatch, capsys) -> None:
    code, _ = _run_list_mo(monkeypatch, ["list", "mo", "uni/tn-common"], {"imdata": []})
    assert code == 0
    assert capsys.readouterr().out == ""


def test_list_mo_reports_a_daemon_error(monkeypatch, capsys) -> None:
    _raise_request(monkeypatch, NotLoggedInError("not logged in"))
    assert cli.main(["list", "mo", "uni"]) == 1
    assert "run 'a4i login'" in capsys.readouterr().err


# -- search -----------------------------------------------------------------
#
# Neither search nor describe reaches the fabric, so every one of these runs
# against the dictionary that ships with a4i and none of them needs a transport.


def test_search_reports_no_match_as_a_failure(capsys) -> None:
    # Unlike 'list class', which answers a prefix and may answer with nothing,
    # a search is asked on the belief that something is there.
    assert cli.main(["search", "zzzznope"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no classes match 'zzzznope'" in captured.err


def test_search_says_on_stderr_what_the_limit_left_out(capsys) -> None:
    assert cli.main(["search", "bridge domain", "--limit", "3"]) == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 3
    # The note must not land on stdout: these three lines are about to be piped.
    assert "matches, showing 3" in captured.err
    assert "--limit 0" in captured.err


def test_search_with_no_limit_says_nothing_about_a_limit(capsys) -> None:
    assert cli.main(["search", "bridge domain", "--limit", "0"]) == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) > 40
    assert captured.err == ""


def test_search_json_carries_the_three_columns(capsys) -> None:
    assert cli.main(["search", "bridge domain", "--limit", "1", "--json"]) == 0
    results = json.loads(capsys.readouterr().out)
    assert results[0]["class"] == "fvBD"
    assert results[0]["label"] == "Bridge Domain"
    assert results[0]["summary"]


# -- describe ---------------------------------------------------------------


def test_describe_json_carries_the_bound_the_layout_leaves_out(capsys) -> None:
    # fvCtx.vrfIndex is bounded 1 to 0 in the MIM, which describes no value at
    # all. The layout drops it (tests/test_output.py); --json is the way to see
    # for yourself that it is really in there.
    assert cli.main(["describe", "fvCtx", "--json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["props"]["vrfIndex"]["validators"] == [{"min": 1, "max": 0}]


def test_describe_rejects_an_unknown_class_with_what_to_try(capsys) -> None:
    assert cli.main(["describe", "fvbd"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "case-sensitive" in captured.err
    assert "Did you mean" in captured.err
    assert "fvBD" in captured.err


def test_describe_json_is_what_the_mcp_tool_serves(capsys) -> None:
    # The layout above is for a person and the JSON is for a model, but they are
    # the one record: a piped 'describe --json' and the MCP tool must not differ.
    from a4i.mcp import tools

    assert cli.main(["describe", "fvBD", "--json"]) == 0
    assert capsys.readouterr().out == tools.call("describe", {"class_name": "fvBD"}) + "\n"


# -- a command group given without its subcommand ---------------------------


@pytest.mark.parametrize("command", ["get", "post", "list", "daemon"])
def test_a_command_without_its_subcommand_shows_its_help(capsys, command) -> None:
    assert cli.main([command]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: a4i {command}")


@pytest.mark.parametrize("command", ["get", "post", "list"])
def test_an_unknown_target_kind_is_refused(capsys, command) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main([command, "dn", "uni/tn-common"])
    assert exit.value.code == 2
    assert "dn" in capsys.readouterr().err


# -- generate-shell-completion ---------------------------------------------


@pytest.mark.parametrize(
    ("shell", "marker"),
    [
        ("zsh", "compdef _a4i_completion a4i"),
        ("bash", "complete -o default -F _a4i_completion a4i"),
        ("fish", "complete -c a4i -f -a '(_a4i_completion)'"),
    ],
)
def test_generate_shell_completion(capsys, shell, marker) -> None:
    assert cli.main(["generate-shell-completion", shell]) == 0
    captured = capsys.readouterr()
    assert marker in captured.out
    # The output is meant to be eval'd, so nothing may leak onto stderr.
    assert captured.err == ""


def test_generate_shell_completion_rejects_an_unknown_shell(capsys) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main(["generate-shell-completion", "tcsh"])
    assert exit.value.code == 2
    assert "tcsh" in capsys.readouterr().err
