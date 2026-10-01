from __future__ import annotations

import io
import json

import pytest

from a4i import _cli as cli
from a4i import _ipc as ipc
from a4i._client import Client
from a4i._merge import merge
from a4i._transport import DaemonTransport


def mo(class_name: str, attributes: dict, children: list | None = None) -> dict:
    body: dict = {"attributes": attributes}
    if children is not None:
        body["children"] = children
    return {class_name: body}


def children(body: dict) -> list[tuple[str, dict]]:
    return [
        (class_name, mo_body["attributes"])
        for child in body["polUni"]["children"]
        for class_name, mo_body in child.items()
    ]


# The DN is rebuilt from the nesting and the "rn" each MO carries, which is the whole of
# what the output says about where an MO sits. An MO carrying no "rn" is given a "?" for
# it.
def walk(body: dict) -> list[tuple[str, str, dict]]:
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
    # may hold.
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
    # Where an MO sits is what the nesting says; an absolute dn would have to be
    # rewritten in every descendant the day a tenant is renamed.
    body = merge(mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1"})]))
    assert body["polUni"]["attributes"]["dn"] == "uni"
    assert all("dn" not in attributes for _, _, attributes in walk(body))


def test_a_root_mo_without_a_dn_hangs_under_uni() -> None:
    assert dns(merge(mo("fvTenant", {"name": "demo"}))) == ["uni/tn-demo"]


def test_a_body_wrapped_in_poluni_is_read_through() -> None:
    wrapped = mo("polUni", {"dn": "uni"}, [mo("fvTenant", {"name": "demo"})])
    assert dns(merge(wrapped)) == ["uni/tn-demo"]


def test_merging_what_merge_wrote_changes_nothing() -> None:
    # The output is an input like any other, so a merged file can be merged again
    # with an override on top.
    once = merge(mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1"})]))
    assert merge(once) == once


def test_the_output_reads_in_dn_order_whatever_order_the_inputs_came_in() -> None:
    # Sorted rather than kept in input order: adding a file then changes only the
    # lines that file contributes.
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
    # Both land on the same DN.
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
    # A dn, an rn and a naming property all resolve to the one key.
    by_dn = mo("fvTenant", {"dn": "uni/tn-t", "descr": "a"})
    by_rn = mo("fvTenant", {"rn": "tn-t", "nameAlias": "b"})
    by_name = mo("fvTenant", {"name": "t", "mtu": "c"})
    ((_, tenant),) = children(merge(by_dn, by_rn, by_name))
    assert tenant == {"rn": "tn-t", "descr": "a", "nameAlias": "b", "name": "t", "mtu": "c"}


def test_the_rn_written_back_is_the_key_merge_settled_on() -> None:
    # What comes out is the last RN of the DN that keyed the merge, never the
    # "dn" the input happened to give.
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
    # Dropping it, as the comparison does, would leave a configuration whose
    # deletions had quietly stopped working.
    ((_, tenant),) = children(merge(mo("fvTenant", {"name": "t", "status": "deleted"})))
    assert tenant["status"] == "deleted"


def test_a_later_status_wins() -> None:
    base = mo("fvTenant", {"name": "t", "status": "deleted"})
    override = mo("fvTenant", {"name": "t", "status": "created,modified"})
    ((_, tenant),) = children(merge(base, override))
    assert tenant["status"] == "created,modified"


def test_a_status_an_override_says_nothing_about_is_inherited() -> None:
    # It merges like any other attribute, so an override that only sets an
    # attribute does not bring a deleted MO back.
    base = mo("fvTenant", {"name": "t", "status": "deleted"})
    override = mo("fvTenant", {"name": "t", "descr": "x"})
    ((_, tenant),) = children(merge(base, override))
    assert tenant["status"] == "deleted"


def test_what_hangs_under_a_deleted_mo_is_still_written() -> None:
    # merge does not read "status": dropping the BD would be dropping
    # configuration on a guess about what the APIC does with the body.
    base = mo("fvTenant", {"name": "t", "status": "deleted"})
    bd = mo("fvBD", {"dn": "uni/tn-t/BD-b", "mtu": "9000"})
    ((_, _, tenant), (_, dn, _)) = walk(merge(base, bd))
    assert tenant["status"] == "deleted"
    assert dn == "uni/tn-t/BD-b"


# -- what is refused -------------------------------------------------------


def test_an_mo_that_names_no_single_object_is_refused() -> None:
    # An fvBD is named by its "name", so without one there is no telling what to
    # merge it with.
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
    # Nothing read this one from a file, so the position is all there is to name
    # it by -- a library caller, or the MCP tool's inline bodies.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvTenant", {"name": "t"}), ["not an mo"])
    assert "configs[1]: [0]" in str(exc.value)


def test_a_malformed_element_is_refused_before_any_dn_is_diagnosed() -> None:
    # A tenant whose children were half skipped would be diagnosed for MOs that
    # nothing describes: a diagnosis of the wrong thing.
    with pytest.raises(ValueError) as exc:
        merge([{"fvBD": None}, mo("fvBD", {"dn": "uni/tn-t/BD-b"})])
    assert "attributes" in str(exc.value)
    assert "nothing describes" not in str(exc.value)


def test_an_input_saying_nothing_is_no_bar_to_the_ones_that_do() -> None:
    # A placeholder file among real ones is not an error.
    assert dns(merge({}, mo("fvTenant", {"name": "t"}), [])) == ["uni/tn-t"]


def test_an_mo_whose_parent_nothing_describes_is_refused() -> None:
    # There is nowhere to nest it, and an invented ancestor would be an MO the
    # POST created that no file asked for.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvBD", {"dn": "uni/tn-t/BD-b", "mtu": "9000"}))
    assert 'nothing describes "uni/tn-t"' in str(exc.value)
    assert 'fvBD at "uni/tn-t/BD-b"' in str(exc.value)


def test_every_dn_the_configuration_is_missing_is_named_at_once() -> None:
    # The line the configuration lacks, not each MO left hanging, and every step
    # of the chain, so one run is enough to fix it.
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
    # A merged body is posted at uni, so an MO outside it has no place in one.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvTenant", {"name": "t"}), mo("fabricNode", {"dn": "topology/pod-1/node-101"}))
    assert '"topology/pod-1/node-101"' in str(exc.value)
    assert "a4i post mo" in str(exc.value)


def test_being_outside_uni_is_reported_before_a_missing_ancestor() -> None:
    # The DN outside uni is the one to fix first; everything under it is missing
    # an ancestor only as a consequence.
    with pytest.raises(ValueError) as exc:
        merge(
            mo("fabricNode", {"dn": "topology/pod-1/node-101"}), mo("fvBD", {"dn": "uni/tn-t/BD-b"})
        )
    assert "topology/pod-1/node-101" in str(exc.value)
    assert "nothing describes" not in str(exc.value)


# -- filling in what nothing describes (--loose) ---------------------------


def test_a_missing_ancestor_is_filled_in_when_asked() -> None:
    # fvBD may hang under fvTenant alone, and "tn-t" is how one is written.
    body = merge(mo("fvBD", {"dn": "uni/tn-t/BD-b", "mtu": "9000"}), loose=True)
    assert walk(body) == [
        ("fvTenant", "uni/tn-t", {"rn": "tn-t"}),
        ("fvBD", "uni/tn-t/BD-b", {"rn": "BD-b", "mtu": "9000"}),
    ]


def test_a_gap_of_several_levels_is_read_from_the_bottom_up() -> None:
    # The fvAEPg gives the fvAp, and that fvAp then gives the fvTenant: the
    # second gap could not have been read off the epg alone.
    body = merge(mo("fvAEPg", {"dn": "uni/tn-t/ap-a/epg-e"}), loose=True)
    assert dns(body) == ["uni/tn-t", "uni/tn-t/ap-a", "uni/tn-t/ap-a/epg-e"]
    assert [class_name for class_name, _, _ in walk(body)] == ["fvTenant", "fvAp", "fvAEPg"]


def test_a_filled_in_mo_carries_its_rn_and_nothing_else() -> None:
    # No "status", so the POST creates it only if the fabric lacks it, and no
    # naming property, the APIC reading "name" off the RN.
    body = merge(mo("fvSubnet", {"dn": "uni/tn-t/BD-b/subnet-[10.0.0.1/24]"}), loose=True)
    tenant, bd, subnet = walk(body)
    assert tenant == ("fvTenant", "uni/tn-t", {"rn": "tn-t"})
    assert bd == ("fvBD", "uni/tn-t/BD-b", {"rn": "BD-b"})
    assert subnet[1] == "uni/tn-t/BD-b/subnet-[10.0.0.1/24]"


def test_nothing_is_filled_in_unless_it_is_asked_for() -> None:
    # A filled-in ancestor is an MO the POST may create that no input asked for,
    # so it is opted into and not out of.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvBD", {"dn": "uni/tn-t/BD-b"}))
    assert 'nothing describes "uni/tn-t"' in str(exc.value)


def test_a_gap_the_dictionary_does_not_settle_is_refused_even_when_asked() -> None:
    # No configurable class is written "xxx" and may hold an fvBD, and this
    # refusal offers no way out, unlike the one above.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvBD", {"dn": "uni/xxx/BD-b"}), loose=True)
    message = str(exc.value)
    assert '"uni/xxx"' in message
    assert "does not settle what class sits there" in message


def test_a_gap_is_filled_in_over_a_class_whose_parents_are_summarised() -> None:
    # The dictionary keeps 8 of tagAnnotation's 2,866 parents, so only reading the
    # gap off what may hang under ctrlrInst settles this.
    body = merge(
        mo("tagAnnotation", {"dn": "uni/controller/annotationKey-[bootx.node.1.cimc]"}),
        loose=True,
    )
    assert walk(body) == [
        ("ctrlrInst", "uni/controller", {"rn": "controller"}),
        (
            "tagAnnotation",
            "uni/controller/annotationKey-[bootx.node.1.cimc]",
            {"rn": "annotationKey-[bootx.node.1.cimc]"},
        ),
    ]


def test_what_the_dictionary_cannot_weigh_narrows_nothing() -> None:
    # A class the dictionary has never heard of, and healthInst, which it marks
    # unconfigurable: neither can appear among the children a gap is read off, so
    # the fvSubnet beside them has to settle it.
    body = merge(
        mo("fooBar", {"dn": "uni/tn-t/BD-b/foo-x"}),
        mo("healthInst", {"dn": "uni/tn-t/BD-b/health"}),
        mo("fvSubnet", {"dn": "uni/tn-t/BD-b/subnet-[10.0.0.1/24]"}),
        loose=True,
    )
    assert [class_name for class_name, _, _ in walk(body)][:2] == ["fvTenant", "fvBD"]


def test_a_gap_is_filled_in_from_its_rn_where_one_class_alone_is_written_that_way() -> None:
    # Nothing under the gap narrows anything, but ctrlrInst is the one
    # configurable class written "controller".
    body = merge(mo("fooBar", {"dn": "uni/controller/foo-x"}), loose=True)
    assert walk(body)[0] == ("ctrlrInst", "uni/controller", {"rn": "controller"})


def test_a_gap_more_than_one_class_is_written_as_is_refused() -> None:
    # fvTenant, plannerMatchTenant and plannerTenantTmpl are all written "tn-t",
    # and an unknown class under the gap rules none of them out.
    with pytest.raises(ValueError) as exc:
        merge(mo("fooBar", {"dn": "uni/tn-t/foo-x"}), loose=True)
    assert "does not settle what class sits there" in str(exc.value)


def test_being_outside_uni_is_refused_however_loose() -> None:
    # Nothing filled in would bring it under uni.
    with pytest.raises(ValueError) as exc:
        merge(mo("fabricNode", {"dn": "topology/pod-1/node-101"}), loose=True)
    assert "a4i post mo" in str(exc.value)


def test_what_was_filled_in_needs_no_filling_in_again() -> None:
    # The output describes every DN on the way down, so merging it back reaches
    # the same body without loose.
    once = merge(mo("fvBD", {"dn": "uni/tn-t/BD-b", "mtu": "9000"}), loose=True)
    assert merge(once) == once


# -- what the container cannot hold ----------------------------------------


def test_an_mo_its_container_cannot_hold_is_refused() -> None:
    # A root MO with no "dn" of its own resolves under uni, where an fvBD cannot
    # hang, so the body is refused before it can be posted.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvBD", {"rn": "BD-aaa"}))
    assert '"uni/BD-aaa"' in str(exc.value)


def test_where_an_mo_belongs_is_named() -> None:
    # The dictionary has fvBD hanging under fvTenant and nowhere else.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvBD", {"dn": "uni/BD-aaa"}))
    assert "it hangs under fvTenant, not polUni" in str(exc.value)


def test_being_held_is_weighed_wherever_the_mo_sits() -> None:
    # Not the roots alone: an fvBD nested in an fvAp is as unpostable as one
    # nested under uni.
    with pytest.raises(ValueError) as exc:
        merge(
            mo(
                "fvTenant",
                {"name": "t"},
                [mo("fvAp", {"name": "a"}, [mo("fvBD", {"name": "b"})])],
            )
        )
    assert '"uni/tn-t/ap-a/BD-b"' in str(exc.value)
    assert "not fvAp" in str(exc.value)


def test_a_container_that_cannot_hold_is_refused_however_loose() -> None:
    # An MO written where it cannot hang is written there whatever its ancestors
    # are, so no filling in would answer this.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvBD", {"dn": "uni/BD-aaa"}), loose=True)
    assert "it hangs under fvTenant, not polUni" in str(exc.value)


def test_a_missing_ancestor_is_reported_before_what_a_container_cannot_hold() -> None:
    # An MO with no container in the index has nothing to be weighed against, so
    # the gap is reported until loose fills it in.
    with pytest.raises(ValueError) as exc:
        merge(mo("fvBD", {"dn": "uni/BD-a"}), mo("fvBD", {"dn": "uni/tn-t/BD-b"}))
    assert 'nothing describes "uni/tn-t"' in str(exc.value)
    assert "hangs under fvTenant, not polUni" not in str(exc.value)


def test_what_the_dictionary_does_not_settle_is_held_by_anything() -> None:
    # A class the dictionary has never heard of, and healthInst, which it marks
    # unconfigurable: a record lists its configurable children alone, so neither
    # could appear there however right its place is.
    body = merge(
        mo("fvTenant", {"name": "t"}, [mo("fooBar", {"rn": "foo-x"})]),
        mo("healthInst", {"dn": "uni/tn-t/health"}),
    )
    assert dns(body) == ["uni/tn-t", "uni/tn-t/foo-x", "uni/tn-t/health"]


def test_a_container_the_dictionary_does_not_know_holds_anything() -> None:
    # An fvBD under a class the dictionary has never heard of is not something it
    # can weigh.
    body = merge(mo("fooBar", {"rn": "foo-x"}, [mo("fvBD", {"name": "b"})]))
    assert dns(body) == ["uni/foo-x", "uni/foo-x/BD-b"]


def test_a_class_written_under_itself_is_held() -> None:
    # vnsDevFolder is one of the few classes the model has containing its own kind.
    body = merge(
        mo("fvTenant", {"dn": "uni/tn-t"}),
        mo("vnsLDevVip", {"dn": "uni/tn-t/lDevVip-d"}),
        mo("vnsDevFolder", {"dn": "uni/tn-t/lDevVip-d/devFolder-f-key-k"}),
        mo("vnsDevFolder", {"dn": "uni/tn-t/lDevVip-d/devFolder-f-key-k/devFolder-g-key-j"}),
    )
    assert dns(body)[-1] == "uni/tn-t/lDevVip-d/devFolder-f-key-k/devFolder-g-key-j"


# -- a class the dictionary does not know ----------------------------------


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


# What the paths mean is a4i._config's and is verified there. What is left here is
# what only the command can show: that it hands its arguments over, and what it
# does with what comes back.


def test_merge_reports_what_it_could_not_read(capsys, tmp_path) -> None:
    assert cli.main(["merge", _write(tmp_path, "broken.json", "{not json")]) == 1
    err = capsys.readouterr().err
    assert "broken.json" in err
    assert "invalid JSON" in err


def test_merge_reads_stdin_when_given_no_path(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(BASE)))
    assert cli.main(["merge"]) == 0
    assert dns(_merged(capsys)) == ["uni/tn-demo"]


def test_merge_fills_in_a_missing_ancestor_when_told_to(capsys, tmp_path) -> None:
    orphan = {"fvBD": {"attributes": {"dn": "uni/tn-demo/BD-b"}}}
    assert cli.main(["merge", "--loose", _write(tmp_path, "bd.json", orphan)]) == 0
    assert dns(_merged(capsys)) == ["uni/tn-demo", "uni/tn-demo/BD-b"]


def test_merge_names_the_way_out_when_an_ancestor_is_undescribed(capsys, tmp_path) -> None:
    # a4i._merge says what is missing; naming --loose is this command's own to add,
    # and UndescribedError is a ValueError, so a handler that let it fall through
    # to the general one would refuse without the way out.
    orphan = {"fvBD": {"attributes": {"dn": "uni/tn-demo/BD-b"}}}
    assert cli.main(["merge", _write(tmp_path, "bd.json", orphan)]) == 1
    err = capsys.readouterr().err
    assert "nothing describes" in err
    assert "--loose" in err


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


# Either side could be internally consistent and still disagree here -- an rn read as
# though it were absolute, say -- with every unit test passing. So one case runs the
# real pipe.
def test_what_merge_writes_is_what_diff_reads(monkeypatch, capsys, tmp_path) -> None:
    base = _write(
        tmp_path, "10-base.json", {"fvTenant": {"attributes": {"name": "demo", "descr": "no"}}}
    )
    over = _write(
        tmp_path,
        "20-over.json",
        {
            "fvTenant": {
                "attributes": {"name": "demo", "descr": "right"},
                "children": [{"fvBD": {"attributes": {"name": "bd1", "mtu": "9000"}}}],
            }
        },
    )
    assert cli.main(["merge", base, over]) == 0
    merged = capsys.readouterr().out

    def get(target, kind, params, node, *, autostart=True):
        return FABRIC.get(target, {"imdata": []})

    monkeypatch.setattr(ipc, "get", get)
    # The real fetch over the mocked GETs rather than a body written out by hand:
    # half this seam is fetch's reading.
    fabric = Client(_transport=DaemonTransport()).fetch()
    monkeypatch.setattr(ipc, "fabric", lambda: fabric)
    monkeypatch.setattr("sys.stdin", io.StringIO(merged))
    # 0 is the fabric matching what the two files describe between them.
    assert cli.main(["diff"]) == 0
    assert capsys.readouterr().out.strip() == "no differences"


# A flat polUni could not do this: its children each carried a right absolute DN and the
# APIC still refused the body, an fvBD being no child of polUni.
def test_what_merge_writes_is_what_a_post_would_place() -> None:
    from a4i import _dry_run as dry_run
    from a4i._merge import read

    body = merge(
        mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1", "mtu": "9000"})]),
        mo("fvSubnet", {"dn": "uni/tn-demo/BD-bd1/subnet-[10.0.0.1/24]"}),
    )
    # The wrapper is read through rather than stood at: uni is not configuration.
    changes = dry_run.compare(read(body), read([]))
    assert [change.dn for change in changes] == [
        "uni/tn-demo",
        "uni/tn-demo/BD-bd1",
        "uni/tn-demo/BD-bd1/subnet-[10.0.0.1/24]",
    ]


def test_merge_takes_json_text_alongside_objects() -> None:
    tenant = mo("fvTenant", {"name": "t", "descr": "old"})
    body = merge(json.dumps(tenant), mo("fvTenant", {"name": "t", "descr": "new"}))
    assert children(body) == [("fvTenant", {"rn": "tn-t", "name": "t", "descr": "new"})]


def test_merge_names_the_argument_whose_json_is_invalid() -> None:
    with pytest.raises(ValueError, match=r"^configs\[1\]: invalid JSON body"):
        merge(mo("fvTenant", {"name": "t"}), "{")
