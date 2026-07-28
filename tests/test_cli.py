from __future__ import annotations

import argparse
import io
import json

import pytest

from a4i import cli, transport
from a4i.ipc import DaemonError

# -- an unusable socket must not read as "no daemon" ------------------------
#
# logout, daemon status and daemon stop all treat a failed request as "there is
# no daemon", which would otherwise hide a socket this client refuses to use.


def _raise(type: str):
    def request(op: str, *, autostart: bool = True, **kwargs):
        raise DaemonError("/tmp/a4i-0 must be mode 0700, not 0777", type=type)

    return request


@pytest.fixture
def args() -> argparse.Namespace:
    return argparse.Namespace()


@pytest.mark.parametrize(
    "command",
    [cli._cmd_logout, cli._cmd_daemon_status, cli._cmd_daemon_stop],
)
def test_socket_error_is_reported(monkeypatch, capsys, args, command) -> None:
    monkeypatch.setattr(cli, "request", _raise("socket"))
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
    monkeypatch.setattr(cli, "request", _raise("error"))
    assert command(args) == 0
    assert expected in capsys.readouterr().out


# -- an APIC that could not be told the session ended -----------------------


def _reply(data: dict):
    def request(op: str, *, autostart: bool = True, **kwargs):
        return data

    return request


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (cli._cmd_logout, "logged out"),
        (cli._cmd_daemon_stop, "daemon stopped"),
    ],
)
def test_unnotified_apic_warns_but_succeeds(monkeypatch, capsys, args, command, expected) -> None:
    monkeypatch.setattr(cli, "request", _reply({"apic_error": "cannot reach https://apic.test"}))
    assert command(args) == 0
    captured = capsys.readouterr()
    assert expected in captured.out
    assert "APIC not notified: cannot reach https://apic.test" in captured.err


@pytest.mark.parametrize(
    "command",
    [cli._cmd_logout, cli._cmd_daemon_stop],
)
def test_a_notified_apic_warns_about_nothing(monkeypatch, capsys, args, command) -> None:
    monkeypatch.setattr(cli, "request", _reply({"apic_error": None}))
    assert command(args) == 0
    assert capsys.readouterr().err == ""


# -- get -------------------------------------------------------------------


def _run_get(monkeypatch, argv: list[str]) -> dict:
    """Parse a get command line and return the kwargs it sends to the daemon."""

    sent: dict = {}

    def request(op: str, *, autostart: bool = True, **kwargs):
        sent["op"] = op
        sent.update(kwargs)
        return {"imdata": []}

    monkeypatch.setattr(transport, "request", request)
    assert cli.main(argv) == 0
    return sent


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
    monkeypatch.setattr(transport, "request", _raise("error"))
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

    def request(op: str, *, autostart: bool = True, **kwargs):
        sent.append({"op": op, **kwargs})
        return TENANT if response is None else response

    monkeypatch.setattr(transport, "request", request)
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
    monkeypatch.setattr(transport, "request", _raise("not_logged_in"))
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
    monkeypatch.setattr(transport, "request", _raise("error"))
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

# The configuration that describes FABRIC exactly, and one that gets a single
# attribute of it wrong.
INTENDED = {
    "fvTenant": {
        "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        "children": [{"fvBD": {"attributes": {"name": "bd1", "mtu": "1500"}}}],
    }
}
WRONG = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "descr": "wrong"}}}


def _run_diff(monkeypatch, argv: list[str]) -> tuple[int, list[dict]]:
    """Run a diff command line and return its exit code and the requests made."""

    sent: list[dict] = []

    def request(op: str, *, autostart: bool = True, **kwargs):
        sent.append({"op": op, **kwargs})
        return FABRIC.get(kwargs.get("target"), {"imdata": []})

    monkeypatch.setattr(transport, "request", request)
    return cli.main(argv), sent


def _write(path, name: str, body) -> str:
    file = path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(body if isinstance(body, str) else json.dumps(body))
    return str(file)


def test_diff_lists_uni_then_fetches_each_top_level_subtree(monkeypatch, tmp_path) -> None:
    config = _write(tmp_path, "tn-demo.json", INTENDED)
    _, sent = _run_diff(monkeypatch, ["diff", config])
    assert [request["op"] for request in sent] == ["get", "get"]
    # Every DN travels bare, with "mo" alongside it saying how to read it.
    assert sent[0]["target"] == "uni"
    assert sent[0]["kind"] == "mo"
    assert sent[0]["params"] == {"query-target": "children", "rsp-prop-include": "config-only"}
    assert sent[1]["target"] == "uni/tn-demo"
    assert sent[1]["params"] == {"rsp-subtree": "full", "rsp-prop-include": "config-only"}


def test_diff_reports_a_clean_fabric_with_a_clean_exit(monkeypatch, capsys, tmp_path) -> None:
    config = _write(tmp_path, "tn-demo.json", INTENDED)
    code, _ = _run_diff(monkeypatch, ["diff", config])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_reports_an_attribute_the_fabric_has_and_the_configuration_lacks(
    monkeypatch, capsys, tmp_path
) -> None:
    quiet = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""}}}
    config = _write(tmp_path, "tn-demo.json", quiet)
    code, _ = _run_diff(monkeypatch, ["diff", config])
    out = capsys.readouterr().out
    # The BD is not mentioned at all, so it is extra; nothing is deleted.
    assert "- fvBD uni/tn-demo/BD-bd1" in out
    assert "1 missing, 0 modified, 1 extra" not in out
    assert "0 missing, 0 modified, 1 extra" in out
    assert code == 2


def test_diff_reports_a_changed_attribute(monkeypatch, capsys, tmp_path) -> None:
    changed = {
        "fvTenant": {
            "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
            "children": [{"fvBD": {"attributes": {"name": "bd1", "mtu": "9000"}}}],
        }
    }
    code, _ = _run_diff(monkeypatch, ["diff", _write(tmp_path, "c.json", changed)])
    out = capsys.readouterr().out
    assert "~ fvBD uni/tn-demo/BD-bd1" in out
    assert '~ mtu: "1500" -> "9000"' in out
    assert code == 2


def test_diff_reads_a_directory_of_json_in_path_order(monkeypatch, capsys, tmp_path) -> None:
    # The later file corrects the descr the earlier one gets wrong.
    _write(tmp_path, "10-base.json", WRONG)
    _write(tmp_path, "20-rest.json", INTENDED)
    code, _ = _run_diff(monkeypatch, ["diff", str(tmp_path)])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_walks_a_directory_recursively_and_skips_what_is_not_json(
    monkeypatch, capsys, tmp_path
) -> None:
    _write(tmp_path, "sub/tn-demo.json", INTENDED)
    _write(tmp_path, "README.md", "not json at all")
    _write(tmp_path, "tn-demo.json.bak", "{broken")
    code, _ = _run_diff(monkeypatch, ["diff", str(tmp_path)])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_reads_a_named_file_whatever_it_is_called(monkeypatch, capsys, tmp_path) -> None:
    config = _write(tmp_path, "intended.txt", INTENDED)
    code, _ = _run_diff(monkeypatch, ["diff", config])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_reads_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(INTENDED)))
    code, _ = _run_diff(monkeypatch, ["diff", "-"])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_takes_the_arguments_in_the_order_given(monkeypatch, capsys, tmp_path) -> None:
    wrong = _write(tmp_path, "a.json", WRONG)
    right = _write(tmp_path, "b.json", INTENDED)
    assert _run_diff(monkeypatch, ["diff", wrong, right])[0] == 0
    capsys.readouterr()
    # The other way round, the wrong value is the one that survives.
    code, _ = _run_diff(monkeypatch, ["diff", right, wrong])
    assert '~ descr: "" -> "wrong"' in capsys.readouterr().out
    assert code == 2


def test_diff_expands_a_subtree_on_request(monkeypatch, capsys, tmp_path) -> None:
    config = _write(tmp_path, "empty.json", {"fvTenant": {"attributes": {"dn": "uni/tn-other"}}})
    code, _ = _run_diff(monkeypatch, ["diff", config, "--expand"])
    out = capsys.readouterr().out
    assert "- fvTenant uni/tn-demo" in out
    assert "- fvBD uni/tn-demo/BD-bd1" in out
    assert code == 2


def test_diff_summarises_a_subtree_without_expand(monkeypatch, capsys, tmp_path) -> None:
    config = _write(tmp_path, "empty.json", {"fvTenant": {"attributes": {"dn": "uni/tn-other"}}})
    _run_diff(monkeypatch, ["diff", config])
    out = capsys.readouterr().out
    assert "- fvTenant uni/tn-demo  (extra: 1 child MO)" in out
    assert "BD-bd1" not in out


def test_diff_leaves_an_excluded_subtree_out_of_the_report(monkeypatch, capsys, tmp_path) -> None:
    # The configuration says nothing about the BD, which would make it extra.
    quiet = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": ""}}}
    config = _write(tmp_path, "tn-demo.json", quiet)
    code, _ = _run_diff(monkeypatch, ["diff", config, "--exclude", "uni/tn-demo/BD-bd1"])
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_takes_exclude_more_than_once(monkeypatch, capsys, tmp_path) -> None:
    # tn-demo would be extra and tn-other missing; excluding both leaves nothing.
    config = _write(tmp_path, "other.json", {"fvTenant": {"attributes": {"dn": "uni/tn-other"}}})
    argv = ["diff", config, "--exclude", "uni/tn-demo", "--exclude", "uni/tn-other"]
    code, _ = _run_diff(monkeypatch, argv)
    assert capsys.readouterr().out.strip() == "no differences"
    assert code == 0


def test_diff_still_fetches_an_excluded_subtree(monkeypatch, tmp_path) -> None:
    # The exclusion narrows the comparison, not the fetch: the fabric is read the
    # same way whether or not anything is left out of the report.
    config = _write(tmp_path, "tn-demo.json", INTENDED)
    _, sent = _run_diff(monkeypatch, ["diff", config, "--exclude", "uni/tn-demo"])
    assert [request["target"] for request in sent] == ["uni", "uni/tn-demo"]


def test_diff_rejects_an_exclude_that_names_no_mo_at_all(monkeypatch, capsys, tmp_path) -> None:
    config = _write(tmp_path, "tn-demo.json", INTENDED)
    assert _run_diff(monkeypatch, ["diff", config, "--exclude", "/"])[0] == 1
    assert "cannot be empty" in capsys.readouterr().err


def test_diff_rejects_a_file_it_cannot_parse(monkeypatch, capsys, tmp_path) -> None:
    config = _write(tmp_path, "broken.json", "{not json")
    assert _run_diff(monkeypatch, ["diff", config])[0] == 1
    err = capsys.readouterr().err
    assert "broken.json" in err
    assert "invalid JSON" in err


def test_diff_reports_a_path_that_is_not_there(monkeypatch, capsys, tmp_path) -> None:
    assert _run_diff(monkeypatch, ["diff", str(tmp_path / "gone.json")])[0] == 1
    assert "gone.json" in capsys.readouterr().err


def test_diff_refuses_an_mo_it_cannot_tell_from_the_ones_on_the_fabric(
    monkeypatch, capsys, tmp_path
) -> None:
    # The fabric names an fvBD by its "name", and this one gives none.
    unresolvable = {
        "fvTenant": {
            "attributes": {"dn": "uni/tn-demo"},
            "children": [{"fvBD": {"attributes": {"mtu": "9000"}}}],
        }
    }
    code, _ = _run_diff(monkeypatch, ["diff", _write(tmp_path, "c.json", unresolvable)])
    captured = capsys.readouterr()
    assert "fvBD under uni/tn-demo" in captured.err
    # Nothing is reported: a partial comparison would read as a clean fabric.
    assert captured.out == ""
    assert code == 1


def test_diff_reports_a_daemon_error(monkeypatch, capsys, tmp_path) -> None:
    config = _write(tmp_path, "tn-demo.json", INTENDED)
    monkeypatch.setattr(transport, "request", _raise("not_logged_in"))
    assert cli.main(["diff", config]) == 1
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

    def request(op: str, *, autostart: bool = True, **kwargs):
        sent.append({"op": op, **kwargs})
        return response

    monkeypatch.setattr(transport, "request", request)
    return cli.main(argv), sent


CHILDREN = {
    "totalCount": "3",
    "imdata": [
        {"fvTenant": {"attributes": {"dn": "uni/tn-infra"}}},
        {"fvTenant": {"attributes": {"dn": "uni/tn-common"}}},
        {"infraInfra": {"attributes": {"dn": "uni/infra"}}},
    ],
}


def test_list_mo_asks_for_the_children_by_name_only(monkeypatch, capsys) -> None:
    code, sent = _run_list_mo(monkeypatch, ["list", "mo", "uni"], CHILDREN)
    assert code == 0
    assert sent == [
        {
            "op": "get",
            "target": "uni",
            "kind": "mo",
            "params": {"query-target": "children", "rsp-prop-include": "naming-only"},
            "node": None,
        }
    ]
    # Sorted, bare, one per line: a line of this is what "a4i get mo" takes.
    assert capsys.readouterr().out.split() == ["uni/infra", "uni/tn-common", "uni/tn-infra"]


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
    monkeypatch.setattr(transport, "request", _raise("not_logged_in"))
    assert cli.main(["list", "mo", "uni"]) == 1
    assert "run 'a4i login'" in capsys.readouterr().err


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
