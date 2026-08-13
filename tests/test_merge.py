from __future__ import annotations

import io
import json
import pathlib

import pytest

from a4i import cli, ipc
from a4i.merge import merge


def mo(class_name: str, attributes: dict, children: list | None = None) -> dict:
    body: dict = {"attributes": attributes}
    if children is not None:
        body["children"] = children
    return {class_name: body}


def children(body: dict) -> list[tuple[str, dict]]:
    """Return the merged MOs as (class name, attributes) pairs, in output order."""

    return [
        (class_name, mo_body["attributes"])
        for child in body["polUni"]["children"]
        for class_name, mo_body in child.items()
    ]


def dns(body: dict) -> list[str]:
    return [attributes["dn"] for _, attributes in children(body)]


# -- the shape of the output -----------------------------------------------


def test_the_output_is_a_poluni_that_says_where_it_goes() -> None:
    # A merged file found on its own has to name its own POST target.
    body = merge(mo("fvTenant", {"name": "demo"}))
    assert body["polUni"]["attributes"] == {"dn": "uni"}


def test_every_mo_carries_its_own_absolute_dn() -> None:
    # The nesting is gone: what placed each MO was the DN it resolved to.
    body = merge(mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1"})]))
    assert dns(body) == ["uni/tn-demo", "uni/tn-demo/BD-bd1"]
    assert all("children" not in child[1] for child in children(body))


def test_a_root_mo_without_a_dn_hangs_under_uni() -> None:
    assert dns(merge(mo("fvTenant", {"name": "demo"}))) == ["uni/tn-demo"]


def test_a_body_wrapped_in_poluni_is_read_through() -> None:
    wrapped = mo("polUni", {"dn": "uni"}, [mo("fvTenant", {"name": "demo"})])
    assert dns(merge(wrapped)) == ["uni/tn-demo"]


def test_merging_what_merge_wrote_changes_nothing() -> None:
    # The output is an input like any other, which is what lets a merged file be
    # merged again with an override on top.
    once = merge(mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1"})]))
    assert merge(once) == once


def test_the_output_reads_in_dn_order_whatever_order_the_inputs_came_in() -> None:
    # Sorted rather than kept in input order, so the output is a function of the
    # input set: adding a file changes only the lines that file contributes.
    body = merge(mo("fvTenant", {"name": "z"}), mo("fvTenant", {"name": "a"}))
    assert dns(body) == ["uni/tn-a", "uni/tn-z"]


def test_a_parent_comes_before_its_children() -> None:
    # It falls out of the DN order, a DN being a prefix of the DNs under it, and
    # it is what a POST of the merged body needs.
    body = merge(mo("fvTenant", {"name": "t"}, [mo("fvBD", {"name": "b"})]))
    assert dns(body) == ["uni/tn-t", "uni/tn-t/BD-b"]


# -- what merging means ----------------------------------------------------


def test_one_mo_written_across_two_inputs_becomes_one() -> None:
    # Both land on the same DN, so a base and an override that each say part of
    # the MO land on the one MO.
    base = mo("fvTenant", {"name": "t"}, [mo("fvCtx", {"name": "v1", "pcEnfPref": "enforced"})])
    override = mo("fvTenant", {"name": "t"}, [mo("fvCtx", {"name": "v1", "descr": "x"})])
    ((_, tenant), (_, ctx)) = children(merge(base, override))
    assert tenant["dn"] == "uni/tn-t"
    assert ctx == {"dn": "uni/tn-t/ctx-v1", "name": "v1", "pcEnfPref": "enforced", "descr": "x"}


def test_a_later_input_wins_attribute_by_attribute() -> None:
    base = mo("fvTenant", {"name": "t", "descr": "old", "nameAlias": "kept"})
    override = mo("fvTenant", {"name": "t", "descr": "new"})
    ((_, tenant),) = children(merge(base, override))
    assert tenant == {"dn": "uni/tn-t", "name": "t", "descr": "new", "nameAlias": "kept"}


def test_an_mo_is_the_same_mo_however_each_input_named_it() -> None:
    # A dn, an rn and a naming property all resolve to the one key, so the three
    # ways of writing the same MO merge rather than piling up.
    by_dn = mo("fvTenant", {"dn": "uni/tn-t", "descr": "a"})
    by_rn = mo("fvTenant", {"rn": "tn-t", "nameAlias": "b"})
    by_name = mo("fvTenant", {"name": "t", "mtu": "c"})
    ((_, tenant),) = children(merge(by_dn, by_rn, by_name))
    assert tenant == {"dn": "uni/tn-t", "descr": "a", "nameAlias": "b", "name": "t", "mtu": "c"}


def test_the_dn_and_rn_an_input_gave_are_not_carried_through() -> None:
    # The answer to "which MO" is the dn merge writes; a second, relative way of
    # saying it would leave the body with two answers, only one of them merged.
    ((_, tenant),) = children(merge(mo("fvTenant", {"rn": "tn-t", "name": "t"})))
    assert "rn" not in tenant
    assert tenant["dn"] == "uni/tn-t"


def test_what_the_apic_says_about_an_mo_is_dropped() -> None:
    ((_, tenant),) = children(merge(mo("fvTenant", {"name": "t", "childAction": ""})))
    assert "childAction" not in tenant


def test_a_non_string_attribute_is_carried_as_the_string_aci_would_use() -> None:
    ((_, bd),) = children(merge(mo("fvBD", {"dn": "uni/tn-t/BD-b", "mtu": 9000})))
    assert bd["mtu"] == "9000"


# -- status ----------------------------------------------------------------


def test_status_survives_the_merge() -> None:
    # Dropping it, as the comparison does, would leave a merged configuration
    # whose deletions had quietly stopped working.
    ((_, tenant),) = children(merge(mo("fvTenant", {"name": "t", "status": "deleted"})))
    assert tenant["status"] == "deleted"


def test_a_later_status_wins() -> None:
    base = mo("fvTenant", {"name": "t", "status": "deleted"})
    override = mo("fvTenant", {"name": "t", "status": "created,modified"})
    ((_, tenant),) = children(merge(base, override))
    assert tenant["status"] == "created,modified"


def test_a_status_an_override_says_nothing_about_is_inherited() -> None:
    # It merges like any other attribute, which cuts both ways: an override that
    # only sets an attribute does not bring a deleted MO back.
    base = mo("fvTenant", {"name": "t", "status": "deleted"})
    override = mo("fvTenant", {"name": "t", "descr": "x"})
    ((_, tenant),) = children(merge(base, override))
    assert tenant["status"] == "deleted"


# -- what is refused -------------------------------------------------------


def test_an_mo_that_names_no_single_object_is_refused() -> None:
    # An fvBD is named by its "name", and without one there is no telling which
    # MO the input meant, so no telling what to merge it with.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvTenant", {"name": "t"}, [mo("fvBD", {"mtu": "9000"})]))
    assert "fvBD under uni/tn-t" in str(exc.value)


def test_one_run_names_every_mo_that_has_to_be_fixed() -> None:
    config = mo(
        "fvTenant",
        {"name": "t"},
        [mo("fvBD", {"mtu": "9000"}), mo("fvCtx", {"descr": "x"})],
    )
    with pytest.raises(ValueError) as exc:
        merge(config)
    assert "fvBD under uni/tn-t" in str(exc.value)
    assert "fvCtx under uni/tn-t" in str(exc.value)


def test_a_configuration_describing_no_mo_is_refused() -> None:
    # What this looks like in practice is a path that pointed at nothing.
    with pytest.raises(ValueError) as exc:
        merge()
    assert "empty" in str(exc.value)


@pytest.mark.parametrize("nothing", [{}, [], {"polUni": {"attributes": {"dn": "uni"}}}])
def test_an_input_describing_no_mo_is_refused_too(nothing) -> None:
    with pytest.raises(ValueError):
        merge(nothing)


def test_an_input_saying_nothing_is_no_bar_to_the_ones_that_do() -> None:
    # A placeholder file among real ones is not an error: what is judged is what
    # the inputs describe between them.
    assert dns(merge({}, mo("fvTenant", {"name": "t"}), [])) == ["uni/tn-t"]


# -- the command line ------------------------------------------------------


def _write(path, name: str, body) -> str:
    file = path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(body if isinstance(body, str) else json.dumps(body))
    return str(file)


def _merged(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


BASE = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "descr": "wrong"}}}
OVERRIDE = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "descr": "right"}}}


def test_merge_prints_the_body_to_stdout(capsys, tmp_path) -> None:
    assert cli.main(["merge", _write(tmp_path, "tn.json", BASE)]) == 0
    assert dns(_merged(capsys)) == ["uni/tn-demo"]


# What the paths mean -- a directory read in order, a named file, stdin, a file
# that is not JSON -- is a4i.config's, and is verified there. What is left here
# is what only the command can show: that it hands its arguments over, and what
# it does with what comes back.


def test_merge_reports_what_it_could_not_read(capsys, tmp_path) -> None:
    assert cli.main(["merge", _write(tmp_path, "broken.json", "{not json")]) == 1
    err = capsys.readouterr().err
    assert "broken.json" in err
    assert "invalid JSON" in err


def test_merge_reports_a_directory_holding_no_configuration(capsys, tmp_path) -> None:
    _write(tmp_path, "README.md", "not json at all")
    assert cli.main(["merge", str(tmp_path)]) == 1
    assert "empty" in capsys.readouterr().err


def test_merge_writes_to_a_file_when_asked(capsys, tmp_path) -> None:
    out = tmp_path / "merged.json"
    assert cli.main(["merge", _write(tmp_path, "tn.json", BASE), "-o", str(out)]) == 0
    assert capsys.readouterr().out == ""
    assert dns(json.loads(out.read_text())) == ["uni/tn-demo"]


def test_merge_names_the_way_out_when_it_refuses_to_overwrite(capsys, tmp_path) -> None:
    # a4i.config raises FileExistsError and names the path; naming --force is
    # this command's own to add, and it is an OSError, so a handler that let it
    # fall through to the general one would still say "already exists" and
    # leave the reader with nowhere to go.
    intended = _write(tmp_path, "tn.json", BASE)
    assert cli.main(["merge", intended, "-o", intended]) == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--force" in err
    assert json.loads(pathlib.Path(intended).read_text()) == BASE


def test_merge_overwrites_when_forced(tmp_path) -> None:
    intended = _write(tmp_path, "tn.json", BASE)
    assert cli.main(["merge", intended, "-o", intended, "--force"]) == 0
    assert dns(json.loads(pathlib.Path(intended).read_text())) == ["uni/tn-demo"]


def test_merge_reads_everything_before_it_writes_anything(tmp_path) -> None:
    # The output landing on an input is only survivable because the write comes
    # last: the merged body still carries what the overwritten file said.
    first = _write(tmp_path, "10-base.json", BASE)
    _write(tmp_path, "20-rest.json", OVERRIDE)
    assert cli.main(["merge", str(tmp_path), "-o", first, "--force"]) == 0
    ((_, tenant),) = children(json.loads(pathlib.Path(first).read_text()))
    assert tenant["descr"] == "right"


# -- merge into diff -------------------------------------------------------

# The fabric the mocked daemon serves: uni holds one tenant, fetched whole.
FABRIC = {
    "uni": {"imdata": [{"fvTenant": {"attributes": {"dn": "uni/tn-demo"}}}]},
    "uni/tn-demo": {
        "imdata": [
            {
                "fvTenant": {
                    "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "right"},
                    "children": [
                        {
                            "fvBD": {
                                "attributes": {
                                    "dn": "uni/tn-demo/BD-bd1",
                                    "name": "bd1",
                                    "mtu": "9000",
                                }
                            }
                        }
                    ],
                }
            }
        ]
    },
}


def test_what_merge_writes_is_what_diff_reads(monkeypatch, capsys, tmp_path) -> None:
    """The one seam the two sides' own tests cannot see between them.

    merge writes absolute DNs into a flat polUni, and diff resolves what it is
    given from uni downwards. Either side could be internally consistent and
    still disagree here -- a relative rn where an absolute dn was meant, say --
    and every unit test would go on passing. So one case runs the real pipe.
    """

    _write(tmp_path, "10-base.json", {"fvTenant": {"attributes": {"name": "demo", "descr": "no"}}})
    _write(
        tmp_path,
        "20-over.json",
        {
            "fvTenant": {
                "attributes": {"name": "demo", "descr": "right"},
                "children": [{"fvBD": {"attributes": {"name": "bd1", "mtu": "9000"}}}],
            }
        },
    )
    merged = tmp_path / "merged.json"
    assert cli.main(["merge", str(tmp_path), "-o", str(merged)]) == 0

    def get(target, kind, params, node, *, autostart=True):
        return FABRIC.get(target, {"imdata": []})

    monkeypatch.setattr(ipc, "get", get)
    monkeypatch.setattr("sys.stdin", io.StringIO(merged.read_text()))
    # 0 is the fabric matching what the two files describe between them: the
    # override's descr won, and the BD it added is on the fabric.
    assert cli.main(["diff"]) == 0
    assert capsys.readouterr().out.strip() == "no differences"
