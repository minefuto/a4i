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
    """Return the MOs directly under the wrapper as (class name, attributes) pairs."""

    return [
        (class_name, mo_body["attributes"])
        for child in body["polUni"]["children"]
        for class_name, mo_body in child.items()
    ]


def walk(body: dict) -> list[tuple[str, str, dict]]:
    """Return (class name, DN, attributes) of every merged MO, parents first.

    The DN is rebuilt from the nesting and the "rn" each MO carries, which is
    the whole of what the output says about where an MO sits. An MO carrying no
    "rn" is given a "?" for it, so a test can tell one apart from an MO whose
    RN merge decided not to write.
    """

    def below(mos, parent: str) -> list[tuple[str, str, dict]]:
        found = []
        for child in mos or []:
            for class_name, mo_body in child.items():
                attributes = mo_body["attributes"]
                dn = f"{parent}/{attributes.get('rn', '?')}"
                found.append((class_name, dn, attributes))
                found.extend(below(mo_body.get("children"), dn))
        return found

    wrapper = body["polUni"]
    return below(wrapper["children"], wrapper["attributes"]["dn"])


def dns(body: dict) -> list[str]:
    return [dn for _, dn, _ in walk(body)]


# -- the shape of the output -----------------------------------------------


def test_the_output_is_a_poluni_that_says_where_it_goes() -> None:
    # A merged file found on its own has to name its own POST target.
    body = merge(mo("fvTenant", {"name": "demo"}))
    assert body["polUni"]["attributes"] == {"dn": "uni"}


def test_every_mo_is_written_inside_the_mo_it_hangs_off() -> None:
    # The one shape a POST takes: the APIC reads a child against what its parent
    # may hold, so a BD written beside its tenant is refused however right its
    # DN is.
    body = merge(mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1"})]))
    assert body == {
        "polUni": {
            "attributes": {"dn": "uni"},
            "children": [
                {
                    "fvTenant": {
                        "attributes": {"rn": "tn-demo", "name": "demo"},
                        "children": [{"fvBD": {"attributes": {"rn": "BD-bd1", "name": "bd1"}}}],
                    }
                }
            ],
        }
    }


def test_an_mo_with_nothing_under_it_carries_no_children_key() -> None:
    body = merge(mo("fvTenant", {"name": "demo"}))
    assert body["polUni"]["children"] == [
        {"fvTenant": {"attributes": {"rn": "tn-demo", "name": "demo"}}}
    ]


def test_only_the_wrapper_carries_a_dn() -> None:
    # Where an MO sits is what the nesting says. A second, absolute way of
    # saying it would have to be rewritten in every descendant the day a tenant
    # is renamed.
    body = merge(mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1"})]))
    assert body["polUni"]["attributes"]["dn"] == "uni"
    assert all("dn" not in attributes for _, _, attributes in walk(body))


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


def test_a_deep_mo_is_placed_under_the_whole_chain() -> None:
    config = mo(
        "fvTenant",
        {"name": "t"},
        [mo("fvBD", {"name": "b"}, [mo("fvSubnet", {"ip": "10.0.0.1/24"})])],
    )
    assert dns(merge(config)) == [
        "uni/tn-t",
        "uni/tn-t/BD-b",
        "uni/tn-t/BD-b/subnet-[10.0.0.1/24]",
    ]


# -- what merging means ----------------------------------------------------


def test_one_mo_written_across_two_inputs_becomes_one() -> None:
    # Both land on the same DN, so a base and an override that each say part of
    # the MO land on the one MO.
    base = mo("fvTenant", {"name": "t"}, [mo("fvCtx", {"name": "v1", "pcEnfPref": "enforced"})])
    override = mo("fvTenant", {"name": "t"}, [mo("fvCtx", {"name": "v1", "descr": "x"})])
    ((_, tenant_dn, _), (_, ctx_dn, ctx)) = walk(merge(base, override))
    assert tenant_dn == "uni/tn-t"
    assert ctx_dn == "uni/tn-t/ctx-v1"
    assert ctx == {"rn": "ctx-v1", "name": "v1", "pcEnfPref": "enforced", "descr": "x"}


def test_a_later_input_wins_attribute_by_attribute() -> None:
    base = mo("fvTenant", {"name": "t", "descr": "old", "nameAlias": "kept"})
    override = mo("fvTenant", {"name": "t", "descr": "new"})
    ((_, tenant),) = children(merge(base, override))
    assert tenant == {"rn": "tn-t", "name": "t", "descr": "new", "nameAlias": "kept"}


def test_an_mo_is_the_same_mo_however_each_input_named_it() -> None:
    # A dn, an rn and a naming property all resolve to the one key, so the three
    # ways of writing the same MO merge rather than piling up.
    by_dn = mo("fvTenant", {"dn": "uni/tn-t", "descr": "a"})
    by_rn = mo("fvTenant", {"rn": "tn-t", "nameAlias": "b"})
    by_name = mo("fvTenant", {"name": "t", "mtu": "c"})
    ((_, tenant),) = children(merge(by_dn, by_rn, by_name))
    assert tenant == {"rn": "tn-t", "descr": "a", "nameAlias": "b", "name": "t", "mtu": "c"}


def test_the_rn_written_back_is_the_key_merge_settled_on() -> None:
    # However an input said which MO it meant, what comes out is the last RN of
    # the DN that keyed the merge -- never the "dn" the input happened to give.
    ((_, tenant),) = children(merge(mo("fvTenant", {"dn": "uni/tn-t", "name": "t"})))
    assert tenant == {"rn": "tn-t", "name": "t"}


def test_what_the_apic_says_about_an_mo_is_dropped() -> None:
    ((_, tenant),) = children(merge(mo("fvTenant", {"name": "t", "childAction": ""})))
    assert "childAction" not in tenant


def test_a_non_string_attribute_is_carried_as_the_string_aci_would_use() -> None:
    config = mo("fvTenant", {"name": "t"}, [mo("fvBD", {"name": "b", "mtu": 9000})])
    _, (_, _, bd) = walk(merge(config))
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


def test_what_hangs_under_a_deleted_mo_is_still_written() -> None:
    # merge does not read "status": dropping the BD because a base deleted its
    # tenant would be dropping configuration on a guess about what the APIC
    # will do with the body.
    base = mo("fvTenant", {"name": "t", "status": "deleted"})
    bd = mo("fvBD", {"dn": "uni/tn-t/BD-b", "mtu": "9000"})
    ((_, _, tenant), (_, dn, _)) = walk(merge(base, bd))
    assert tenant["status"] == "deleted"
    assert dn == "uni/tn-t/BD-b"


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


def test_an_input_not_written_as_aci_expects_is_refused() -> None:
    # Nothing has read this one from a file, so the position is all there is to
    # name it by: a library caller and the MCP tool's inline bodies land here.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvTenant", {"name": "t"}), ["not an mo"])
    assert "configs[1]: [0]" in str(exc.value)


def test_a_malformed_element_is_refused_before_any_dn_is_diagnosed() -> None:
    # A tenant whose children were half skipped would be diagnosed for MOs that
    # nothing describes, which is a diagnosis of the wrong thing.
    with pytest.raises(ValueError) as exc:
        merge([{"fvBD": None}, mo("fvBD", {"dn": "uni/tn-t/BD-b"})])
    assert "attributes" in str(exc.value)
    assert "nothing describes" not in str(exc.value)


def test_an_input_saying_nothing_is_no_bar_to_the_ones_that_do() -> None:
    # A placeholder file among real ones is not an error: what is judged is what
    # the inputs describe between them.
    assert dns(merge({}, mo("fvTenant", {"name": "t"}), [])) == ["uni/tn-t"]


def test_an_mo_whose_parent_nothing_describes_is_refused() -> None:
    # There is nowhere to nest it, and the tenant is not made up: an ancestor
    # invented here would be an MO the POST created that no file asked for.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvBD", {"dn": "uni/tn-t/BD-b", "mtu": "9000"}))
    assert 'nothing describes "uni/tn-t"' in str(exc.value)
    assert 'fvBD at "uni/tn-t/BD-b"' in str(exc.value)


def test_every_dn_the_configuration_is_missing_is_named_at_once() -> None:
    # What is reported is the line the configuration lacks, not each MO left
    # hanging: writing that one line settles all of them. Every step of the
    # chain is named, so one run is enough to fix it.
    with pytest.raises(ValueError) as exc:
        merge(
            mo("fvBD", {"dn": "uni/tn-t/BD-b"}),
            mo("fvBD", {"dn": "uni/tn-t/BD-c"}),
            mo("fvSubnet", {"dn": "uni/tn-u/BD-d/subnet-[10.0.0.1/24]"}),
        )
    message = str(exc.value)
    assert '"uni/tn-t"' in message
    assert '"uni/tn-u"' in message
    assert '"uni/tn-u/BD-d"' in message


def test_an_mo_outside_uni_is_refused() -> None:
    # A merged body is posted at uni, so an MO that does not sit under it has
    # no place in one however well described it is.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvTenant", {"name": "t"}), mo("fabricNode", {"dn": "topology/pod-1/node-101"}))
    assert '"topology/pod-1/node-101"' in str(exc.value)
    assert "a4i post mo" in str(exc.value)


def test_being_outside_uni_is_reported_before_a_missing_ancestor() -> None:
    # The DN outside uni is the input's own doing and the one to fix first;
    # everything under it is missing an ancestor only as a consequence.
    with pytest.raises(ValueError) as exc:
        merge(
            mo("fabricNode", {"dn": "topology/pod-1/node-101"}), mo("fvBD", {"dn": "uni/tn-t/BD-b"})
        )
    assert "topology/pod-1/node-101" in str(exc.value)
    assert "nothing describes" not in str(exc.value)


# -- a class the dictionary does not know ----------------------------------


def test_a_stand_in_rn_is_not_written_back() -> None:
    # a4i.mo.pseudo_rn keys an unknown class by what the body gives, which is no
    # RN the APIC would take. The nesting says where the MO sits, so the body
    # can leave the RN to the APIC to build from the naming property.
    config = mo("fvTenant", {"name": "t"}, [mo("fooBar", {"name": "x", "descr": "y"})])
    _, (_, dn, unknown) = walk(merge(config))
    assert dn == "uni/tn-t/?"
    assert unknown == {"name": "x", "descr": "y"}


def test_an_unknown_class_keeps_an_rn_the_input_spelled_out() -> None:
    config = mo("fvTenant", {"name": "t"}, [mo("fooBar", {"rn": "foo-x", "descr": "y"})])
    _, (_, dn, unknown) = walk(merge(config))
    assert dn == "uni/tn-t/foo-x"
    assert unknown == {"rn": "foo-x", "descr": "y"}


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

    merge nests every MO under the MO it hangs off and names it by its rn, and
    diff resolves what it is given from uni downwards. Either side could be
    internally consistent and still disagree here -- an rn read as though it
    were absolute, say -- and every unit test would go on passing. So one case
    runs the real pipe.
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


def test_what_merge_writes_is_what_a_post_would_place() -> None:
    """The other seam: the body has to land where the configuration meant it to.

    This is what a flat polUni could not do. Its children each carried a right
    absolute DN and the APIC still refused the body, an fvBD being no child of
    polUni. Reading the merged body back the way a POST does says where each MO
    would land.
    """

    from a4i import dry_run

    body = merge(
        mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1", "mtu": "9000"})]),
        mo("fvSubnet", {"dn": "uni/tn-demo/BD-bd1/subnet-[10.0.0.1/24]"}),
    )
    changes = dry_run.compare(body, [], "uni")
    assert [change.dn for change in changes] == [
        "uni",
        "uni/tn-demo",
        "uni/tn-demo/BD-bd1",
        "uni/tn-demo/BD-bd1/subnet-[10.0.0.1/24]",
    ]
