from __future__ import annotations

import pytest

from a4i import diff

# -- the fabric as the APIC would return it, everything under uni ----------


def mo(class_name: str, attributes: dict, children: list | None = None) -> dict:
    body: dict = {"attributes": attributes}
    if children is not None:
        body["children"] = children
    return {class_name: body}


FABRIC = [
    mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [
            mo(
                "fvBD",
                {"dn": "uni/tn-demo/BD-bd1", "name": "bd1", "mtu": "1500"},
                [mo("fvRsCtx", {"dn": "uni/tn-demo/BD-bd1/rsctx", "tnFvCtxName": "v1"})],
            ),
            mo("fvBD", {"dn": "uni/tn-demo/BD-bd2", "name": "bd2", "mtu": "1500"}),
        ],
    ),
    mo(
        "fvTenant",
        {"dn": "uni/tn-common", "name": "common"},
        [mo("fvBD", {"dn": "uni/tn-common/BD-default", "name": "default"})],
    ),
]

# The configuration that describes FABRIC exactly, attribute for attribute.
INTENDED = [
    mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [
            mo(
                "fvBD",
                {"name": "bd1", "mtu": "1500"},
                [mo("fvRsCtx", {"tnFvCtxName": "v1"})],
            ),
            mo("fvBD", {"name": "bd2", "mtu": "1500"}),
        ],
    ),
    mo(
        "fvTenant",
        {"dn": "uni/tn-common", "name": "common"},
        [mo("fvBD", {"name": "default"})],
    ),
]


def compare(*mos, imdata: list | None = None, expand: bool = False, exclude=None) -> list:
    """Compare one configuration made of the MOs given, against FABRIC.

    diff takes a single body, and a list of MOs is one. So the arguments here
    are flattened into that one list rather than passed as several inputs --
    folding several inputs into one is a4i.merge's job, and is tested there.
    """

    config = [one for arg in mos for one in (arg if isinstance(arg, list) else [arg])]
    return diff.compare(
        config, FABRIC if imdata is None else imdata, expand=expand, exclude=exclude
    )


def kinds(changes: list) -> list[tuple[str, str]]:
    return [(change.kind, change.dn) for change in changes]


# -- the baseline ----------------------------------------------------------


def test_a_configuration_matching_the_fabric_shows_no_differences() -> None:
    assert compare(*INTENDED) == []


def test_the_report_reads_in_dn_order_whatever_order_the_inputs_came_in() -> None:
    changes = compare(
        mo("fvTenant", {"dn": "uni/tn-z", "name": "z"}),
        mo("fvTenant", {"dn": "uni/tn-a", "name": "a"}),
    )
    assert kinds(changes) == [
        ("missing", "uni/tn-a"),
        ("extra", "uni/tn-common"),
        ("extra", "uni/tn-demo"),
        ("missing", "uni/tn-z"),
    ]


# -- MOs, both ways --------------------------------------------------------


def test_an_mo_the_fabric_has_and_the_configuration_does_not_is_extra() -> None:
    # tn-common is left out entirely.
    changes = compare(INTENDED[0])
    assert kinds(changes) == [("extra", "uni/tn-common")]


def test_an_extra_subtree_is_reported_as_its_top_mo_with_the_rest_counted() -> None:
    (change,) = compare(INTENDED[0])
    assert change.child_count == 1  # BD-default goes with it
    assert change.class_name == "fvTenant"


def test_an_mo_the_configuration_has_and_the_fabric_does_not_is_missing() -> None:
    wanted = mo("fvTenant", {"dn": "uni/tn-new", "name": "new"}, [mo("fvBD", {"name": "bd9"})])
    changes = compare(*INTENDED, wanted)
    assert kinds(changes) == [("missing", "uni/tn-new")]


def test_a_missing_subtree_is_reported_as_its_top_mo_with_the_rest_counted() -> None:
    wanted = mo(
        "fvTenant",
        {"dn": "uni/tn-new", "name": "new"},
        [mo("fvBD", {"name": "bd9"}, [mo("fvRsCtx", {"tnFvCtxName": "v9"})])],
    )
    (change,) = compare(*INTENDED, wanted)
    assert change.child_count == 2


def test_expand_lists_every_mo_of_a_subtree_instead_of_counting_it() -> None:
    wanted = mo("fvTenant", {"dn": "uni/tn-new", "name": "new"}, [mo("fvBD", {"name": "bd9"})])
    changes = compare(*INTENDED, wanted, expand=True)
    assert kinds(changes) == [("missing", "uni/tn-new"), ("missing", "uni/tn-new/BD-bd9")]
    assert [change.child_count for change in changes] == [0, 0]


def test_expand_lists_every_mo_of_an_extra_subtree_too() -> None:
    changes = compare(INTENDED[0], expand=True)
    assert kinds(changes) == [
        ("extra", "uni/tn-common"),
        ("extra", "uni/tn-common/BD-default"),
    ]


def test_a_missing_mo_carries_the_attributes_the_configuration_asks_for() -> None:
    wanted = mo("fvTenant", {"dn": "uni/tn-new", "name": "new", "descr": "x"})
    (change,) = compare(*INTENDED, wanted)
    assert change.attributes == {"descr": (None, "x"), "name": (None, "new")}


def test_an_extra_mo_carries_the_attributes_the_fabric_has() -> None:
    (change,) = compare(INTENDED[0])
    # dn is identity, not configuration, so it is not among them.
    assert change.attributes == {"name": ("common", None)}


# -- attributes, both ways -------------------------------------------------


def test_an_attribute_with_a_different_value_is_modified() -> None:
    changed = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [
            mo("fvBD", {"name": "bd1", "mtu": "9000"}, [mo("fvRsCtx", {"tnFvCtxName": "v1"})]),
            mo("fvBD", {"name": "bd2", "mtu": "1500"}),
        ],
    )
    (change,) = compare(changed, INTENDED[1])
    assert (change.kind, change.dn) == ("modified", "uni/tn-demo/BD-bd1")
    assert change.attributes == {"mtu": ("1500", "9000")}


def test_an_attribute_the_fabric_has_and_the_configuration_does_not_is_reported() -> None:
    # The BD's mtu is dropped from the configuration; the fabric still has it.
    quiet = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [
            mo("fvBD", {"name": "bd1"}, [mo("fvRsCtx", {"tnFvCtxName": "v1"})]),
            mo("fvBD", {"name": "bd2", "mtu": "1500"}),
        ],
    )
    (change,) = compare(quiet, INTENDED[1])
    assert change.attributes == {"mtu": ("1500", None)}


def test_an_empty_attribute_the_configuration_does_not_mention_is_reported() -> None:
    # The APIC returns an unset attribute as "", and that is a value the
    # configuration has not accounted for.
    tenant = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo"})
    imdata = [mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "descr": ""})]
    (change,) = diff.compare([tenant], imdata)
    assert change.attributes == {"descr": ("", None)}


def test_an_attribute_the_configuration_asks_for_and_the_fabric_lacks_is_reported() -> None:
    tenant = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "nameAlias": "prod"})
    imdata = [mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo"})]
    (change,) = diff.compare([tenant], imdata)
    assert change.attributes == {"nameAlias": (None, "prod")}


def test_identity_attributes_are_never_diffed() -> None:
    tenant = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "status": "created"})
    imdata = [
        mo("fvTenant", {"dn": "uni/tn-demo", "rn": "tn-demo", "name": "demo", "childAction": ""})
    ]
    assert diff.compare([tenant], imdata) == []


def test_attributes_are_reported_in_name_order() -> None:
    tenant = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "descr": "b"})
    imdata = [mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "zzz": "z", "aaa": "a"})]
    (change,) = diff.compare([tenant], imdata)
    assert list(change.attributes) == ["aaa", "descr", "zzz"]


# -- merging ---------------------------------------------------------------


def test_two_inputs_naming_the_same_mo_have_their_children_pooled() -> None:
    # tn-demo split into a file per BD, each carrying the tenant header.
    bd1 = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"name": "bd1", "mtu": "1500"}, [mo("fvRsCtx", {"tnFvCtxName": "v1"})])],
    )
    bd2 = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"name": "bd2", "mtu": "1500"})],
    )
    assert compare(bd1, bd2, INTENDED[1]) == []


def test_a_later_input_wins_the_attributes_it_sets() -> None:
    wrong = mo("fvTenant", {"dn": "uni/tn-demo", "descr": "wrong"})
    assert compare(wrong, *INTENDED) == []
    # The same two the other way round leaves the wrong value standing.
    (change,) = compare(*INTENDED, wrong)
    assert change.attributes == {"descr": ("", "wrong")}


def test_a_list_of_mos_is_one_input() -> None:
    assert compare(INTENDED) == []


# -- resolving DNs ---------------------------------------------------------


def test_a_root_mo_without_a_dn_hangs_under_uni() -> None:
    # The bundled rnFormat for fvTenant is "tn-{name}".
    tenant = mo("fvTenant", {"name": "new"})
    (change,) = compare(*INTENDED, tenant)
    assert change.dn == "uni/tn-new"


def test_a_child_of_a_new_mo_gets_a_dn_of_its_own() -> None:
    # tn-new has no BD of its own, and needs none: the RN format is bundled.
    tenant = mo("fvTenant", {"dn": "uni/tn-new", "name": "new"}, [mo("fvBD", {"name": "bd9"})])
    changes = compare(*INTENDED, tenant, expand=True)
    assert kinds(changes) == [("missing", "uni/tn-new"), ("missing", "uni/tn-new/BD-bd9")]


def test_a_fixed_rn_child_resolves_as_well() -> None:
    # fvRsCtx embeds no attribute value in its RN; every one of them is "rsctx".
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-new", "name": "new"},
        [mo("fvBD", {"name": "bd9"}, [mo("fvRsCtx", {"tnFvCtxName": "v9"})])],
    )
    changes = compare(*INTENDED, tenant, expand=True)
    assert kinds(changes)[-1] == ("missing", "uni/tn-new/BD-bd9/rsctx")


# -- MOs of a class the fabric carries none of -----------------------------


def test_a_class_the_fabric_has_never_seen_is_reported_missing() -> None:
    # Nothing on the fabric can be what this fvCtx means, so it is missing -- and
    # under the DN ACI would give it, which no MO on the fabric had to show.
    tenant = mo("fvTenant", {"dn": "uni/tn-new", "name": "new"}, [mo("fvCtx", {"name": "v1"})])
    changes = compare(*INTENDED, tenant, expand=True)
    assert kinds(changes) == [
        ("missing", "uni/tn-new"),
        ("missing", "uni/tn-new/ctx-v1"),
    ]


def test_two_mos_of_a_class_the_fabric_lacks_stay_two() -> None:
    # Both once landed on uni/tn-demo/? and the second overwrote the first -- an
    # MO lost that way reads as a fabric that matches.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvCtx", {"name": "vrf-a"}), mo("fvCtx", {"name": "vrf-b"})],
    )
    reported = kinds(compare(tenant, INTENDED[1], expand=True))
    assert ("missing", "uni/tn-demo/ctx-vrf-a") in reported
    assert ("missing", "uni/tn-demo/ctx-vrf-b") in reported


def test_a_root_mo_of_a_class_the_fabric_lacks_is_reported_missing() -> None:
    changes = compare(*INTENDED, mo("vzBrCP", {"name": "c1"}), expand=True)
    assert kinds(changes) == [("missing", "uni/brc-c1")]


# -- MOs with no bundled RN format -----------------------------------------


def test_a_class_the_dictionary_lacks_falls_back_to_a_stand_in_rn() -> None:
    # A newer APIC's classes are not in the bundled dictionary, and comparing on
    # what the input gives beats refusing to compare at all.
    tenant = mo("fvTenant", {"dn": "uni/tn-new", "name": "new"}, [mo("fooBar", {"name": "b1"})])
    changes = compare(*INTENDED, tenant, expand=True)
    assert kinds(changes)[-1] == ("missing", "uni/tn-new/fooBar[name=b1]")


def test_two_such_mos_stay_two() -> None:
    # Their stand-in RNs differ, so neither is merged away into the other.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-new", "name": "new"},
        [mo("fooBar", {"prop": "p"}), mo("fooBar", {"prop": "q"})],
    )
    reported = kinds(compare(*INTENDED, tenant, expand=True))
    assert ("missing", "uni/tn-new/fooBar[prop=p]") in reported
    assert ("missing", "uni/tn-new/fooBar[prop=q]") in reported


# -- MOs the input does not name -------------------------------------------


def test_an_mo_the_input_does_not_identify_is_refused() -> None:
    # An fvBD RN is "BD-{name}" and this one gives no name, so it could be bd1 or
    # bd2. Reporting it missing while reporting the real one extra would be worse
    # than saying what the input has to spell out.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    with pytest.raises(ValueError) as exc:
        compare(tenant, INTENDED[1])
    assert "fvBD under uni/tn-demo" in str(exc.value)
    assert "BD-{name}" in str(exc.value)
    assert '"dn" or an "rn"' in str(exc.value)


def test_it_is_refused_even_where_the_fabric_has_no_such_mo() -> None:
    # The RN format settles it without the fabric: there is no MO this could be
    # under tn-new either, but the input still names none.
    tenant = mo("fvTenant", {"dn": "uni/tn-new", "name": "new"}, [mo("fvBD", {"mtu": "9000"})])
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, tenant)
    assert "fvBD under uni/tn-new" in str(exc.value)


def test_one_run_names_every_mo_that_has_to_be_fixed() -> None:
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    with pytest.raises(ValueError) as exc:
        compare(tenant, INTENDED[1], mo("fvTenant", {"descr": "y"}))
    assert "fvBD under uni/tn-demo" in str(exc.value)
    assert "fvTenant under uni" in str(exc.value)


def test_the_children_of_an_unidentified_mo_are_not_named_as_well() -> None:
    # A child's key hangs off its parent's, so naming it only repeats the parent.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"}, [mo("fvRsCtx", {"tnFvCtxName": "x"})])],
    )
    with pytest.raises(ValueError) as exc:
        compare(tenant, INTENDED[1])
    assert str(exc.value).count(" under ") == 1


def test_an_rn_in_the_input_settles_a_class_the_fabric_lacks() -> None:
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-new", "name": "new"},
        [mo("fvCtx", {"name": "v1", "rn": "ctx-v1"})],
    )
    changes = compare(*INTENDED, tenant, expand=True)
    assert kinds(changes)[-1] == ("missing", "uni/tn-new/ctx-v1")


def test_a_body_wrapped_in_poluni_is_read_through() -> None:
    # uni itself is not configuration, so its children are the roots.
    wrapped = mo("polUni", {"dn": "uni"}, INTENDED)
    assert compare(wrapped) == []


def test_a_poluni_wrapper_is_read_through_without_a_dn_as_well() -> None:
    # The class says it is the wrapper, so a POST body that leaves the dn to the
    # URL is read the same way.
    assert compare(mo("polUni", {}, INTENDED)) == []


def test_a_malformed_input_is_refused_rather_than_skipped() -> None:
    # Skipping it would compare against a configuration missing whatever the
    # element was meant to say, and report the fabric extra for carrying it.
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, "not an mo", {"a": {}, "b": {}})
    assert "[2]" in str(exc.value)
    assert "[3]" in str(exc.value)


# -- reading a response back as the configuration it describes -------------
#
# The whole point of "config-only": what the APIC returns for a subtree is what
# that subtree is meant to be, so feeding one straight back has to report
# nothing. It only does if both sides work a DN out the same way, and the APIC
# does not always spell one out on a child.


def test_a_response_compared_against_itself_shows_no_differences() -> None:
    assert diff.compare([mo("polUni", {"dn": "uni"}, FABRIC)], FABRIC) == []


def test_a_child_named_only_by_an_rn_is_read_the_same_on_both_sides() -> None:
    imdata = [
        mo(
            "fvTenant",
            {"dn": "uni/tn-demo", "name": "demo"},
            [mo("fvBD", {"rn": "BD-bd1", "name": "bd1"})],
        )
    ]
    assert diff.compare([mo("polUni", {"dn": "uni"}, imdata)], imdata) == []


def test_a_child_named_by_neither_a_dn_nor_an_rn_is_kept_on_the_fabric_side() -> None:
    # The RN format names it from "name" alone, and it has to be applied to the
    # response as well: dropping the branch would report the whole subtree
    # missing from a fabric that is carrying it.
    imdata = [
        mo(
            "fvTenant",
            {"dn": "uni/tn-demo", "name": "demo"},
            [mo("fvAp", {"name": "ap1"}, [mo("fvAEPg", {"name": "epg1"})])],
        )
    ]
    assert diff.compare([mo("polUni", {"dn": "uni"}, imdata)], imdata) == []


# -- MOs left out of the comparison ----------------------------------------


def test_an_excluded_mo_the_fabric_has_is_not_reported_extra() -> None:
    # tn-common is left out of the configuration, and excluded as well.
    assert compare(INTENDED[0], exclude="uni/tn-common") == []


def test_excluding_an_mo_excludes_everything_under_it() -> None:
    # With expand there is nothing left to summarise a subtree into, so this is
    # where a child that outlived its parent would show.
    assert compare(INTENDED[0], exclude="uni/tn-common", expand=True) == []


def test_an_excluded_mo_the_configuration_asks_for_is_not_reported_missing() -> None:
    # The other side of the same rule: excluding says nothing about the subtree,
    # so an MO in it is not missing either, however loudly the input asks for it.
    wanted = mo("fvTenant", {"dn": "uni/tn-new", "name": "new"}, [mo("fvBD", {"name": "bd9"})])
    assert compare(*INTENDED, wanted, exclude="uni/tn-new", expand=True) == []


def test_an_excluded_mo_both_sides_have_is_not_reported_modified() -> None:
    changed = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [
            mo("fvBD", {"name": "bd1", "mtu": "9000"}, [mo("fvRsCtx", {"tnFvCtxName": "v1"})]),
            mo("fvBD", {"name": "bd2", "mtu": "1500"}),
        ],
    )
    assert compare(changed, INTENDED[1], exclude="uni/tn-demo/BD-bd1") == []


def test_excluding_a_child_leaves_its_parent_compared() -> None:
    # Only the subtree named goes; the MO above it is still held to the
    # configuration.
    changed = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": "changed"},
        [
            mo("fvBD", {"name": "bd1", "mtu": "9000"}, [mo("fvRsCtx", {"tnFvCtxName": "v1"})]),
            mo("fvBD", {"name": "bd2", "mtu": "1500"}),
        ],
    )
    (change,) = compare(changed, INTENDED[1], exclude="uni/tn-demo/BD-bd1")
    assert (change.kind, change.dn) == ("modified", "uni/tn-demo")
    assert change.attributes == {"descr": ("", "changed")}


def test_the_mos_going_with_a_subtree_are_counted_without_the_excluded_ones() -> None:
    # tn-demo carries BD-bd1, its rsctx and BD-bd2. Excluding BD-bd1 takes the
    # rsctx under it along, leaving one MO to go with the tenant.
    (whole,) = compare(INTENDED[1])
    assert whole.child_count == 3
    (pruned,) = compare(INTENDED[1], exclude="uni/tn-demo/BD-bd1")
    assert pruned.child_count == 1


def test_a_dn_that_is_not_an_rn_boundary_excludes_nothing() -> None:
    # "uni/tn-comm" is a prefix of the text of uni/tn-common and the DN of no
    # MO, so it leaves the tenant where it is.
    assert kinds(compare(INTENDED[0], exclude="uni/tn-comm")) == [("extra", "uni/tn-common")]


def test_a_sibling_whose_dn_starts_the_same_way_is_not_excluded() -> None:
    imdata = [
        mo("fvTenant", {"dn": "uni/tn-a", "name": "a"}),
        mo("fvTenant", {"dn": "uni/tn-a2", "name": "a2"}),
    ]
    config = mo("fvTenant", {"dn": "uni/tn-a", "name": "a"})
    changes = compare(config, imdata=imdata, exclude="uni/tn-a")
    assert kinds(changes) == [("extra", "uni/tn-a2")]


def test_a_prefix_ending_inside_a_naming_value_excludes_nothing() -> None:
    # A subnet's RN holds a "/" of its own, so the ancestors are walked rather
    # than the text of the DN matched: this one is an ancestor of nothing.
    imdata = [
        mo(
            "fvTenant",
            {"dn": "uni/tn-x", "name": "x"},
            [mo("fvBD", {"name": "b"}, [mo("fvSubnet", {"ip": "10.0.0.1/24"})])],
        )
    ]
    reported = kinds(
        compare(
            mo("fvTenant", {"dn": "uni/tn-x", "name": "x"}),
            imdata=imdata,
            expand=True,
            exclude="uni/tn-x/BD-b/subnet-[10.0.0.1",
        )
    )
    assert ("extra", "uni/tn-x/BD-b/subnet-[10.0.0.1/24]") in reported


def test_the_naming_value_holding_a_slash_is_excluded_when_named_in_full() -> None:
    imdata = [
        mo(
            "fvTenant",
            {"dn": "uni/tn-x", "name": "x"},
            [mo("fvBD", {"name": "b"}, [mo("fvSubnet", {"ip": "10.0.0.1/24"})])],
        )
    ]
    reported = kinds(
        compare(
            mo("fvTenant", {"dn": "uni/tn-x", "name": "x"}),
            imdata=imdata,
            expand=True,
            exclude="uni/tn-x/BD-b/subnet-[10.0.0.1/24]",
        )
    )
    assert reported == [("extra", "uni/tn-x/BD-b")]


def test_an_excluded_dn_that_matches_nothing_is_accepted() -> None:
    # Excluding what is not there hides no difference, so it is not worth
    # refusing: the same command line can serve a fabric that has the tenant and
    # one that does not.
    assert kinds(compare(INTENDED[0], exclude="uni/tn-typo")) == [("extra", "uni/tn-common")]


def test_one_dn_can_be_given_as_a_string_or_as_a_sequence() -> None:
    # A string is one DN, never a list to split: an ACI naming value can hold a
    # comma, so there is nothing here to separate one DN from the next.
    assert compare(INTENDED[0], exclude=["uni/tn-common"]) == []
    assert compare(INTENDED[0], exclude=("uni/tn-common",)) == []


def test_several_dns_are_excluded_at_once() -> None:
    assert compare(*INTENDED, exclude=["uni/tn-demo", "uni/tn-common"]) == []


def test_a_dn_is_read_with_or_without_its_slashes() -> None:
    # As a DN is read anywhere else: a leading or trailing "/" no longer marks
    # anything.
    assert compare(INTENDED[0], exclude="/uni/tn-common/") == []


def test_excluding_the_root_excludes_the_whole_comparison() -> None:
    # Nothing special is made of uni: it is the ancestor of every MO, so naming
    # it leaves nothing to compare.
    assert compare(*INTENDED, exclude="uni") == []


def test_an_empty_dn_is_refused() -> None:
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, exclude=" / ")
    assert "cannot be empty" in str(exc.value)


def test_an_unidentified_mo_under_an_excluded_one_does_not_stop_the_comparison() -> None:
    # Which fvBD the input meant is a question about a subtree nothing will be
    # reported about, so there is nothing left for the input to settle.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    assert compare(tenant, INTENDED[1], exclude="uni/tn-demo") == []


def test_an_unidentified_mo_outside_the_excluded_subtree_is_still_refused() -> None:
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    with pytest.raises(ValueError) as exc:
        compare(tenant, INTENDED[1], exclude="uni/tn-common")
    assert "fvBD under uni/tn-demo" in str(exc.value)


def test_excluding_one_bd_does_not_excuse_an_unidentified_sibling() -> None:
    # The input names no one fvBD, so it could be the excluded one or another:
    # the exclusion settles nothing.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    with pytest.raises(ValueError):
        compare(tenant, INTENDED[1], exclude="uni/tn-demo/BD-bd1")


# -- MOs left out by a pattern ---------------------------------------------

# Three tenants whose names start alike, which is what a pattern is for and what
# a pattern gets wrong: the fabric has them, the configuration describes prod.
TENANTS = [
    mo("fvTenant", {"dn": "uni/tn-test", "name": "test"}),
    mo("fvTenant", {"dn": "uni/tn-testbed", "name": "testbed"}),
    mo("fvTenant", {"dn": "uni/tn-prod", "name": "prod"}),
]
PROD = mo("fvTenant", {"dn": "uni/tn-prod", "name": "prod"})


def test_a_pattern_excludes_every_mo_whose_rn_it_matches() -> None:
    assert compare(PROD, imdata=TENANTS, exclude="uni/tn-test*") == []


def test_a_star_stands_for_nothing_as_well_as_for_something() -> None:
    # tn-testbed is matched by tn-testbed*, so a name written out in full and a
    # pattern that happens to end in a "*" leave out the same MO.
    reported = kinds(compare(PROD, imdata=TENANTS, exclude="uni/tn-testbed*"))
    assert reported == [("extra", "uni/tn-test")]


def test_an_mo_the_pattern_does_not_match_is_still_compared() -> None:
    changed = mo("fvTenant", {"dn": "uni/tn-prod", "name": "prod", "descr": "changed"})
    (change,) = compare(changed, imdata=TENANTS, exclude="uni/tn-test*")
    assert (change.kind, change.dn) == ("modified", "uni/tn-prod")


def test_a_pattern_excludes_everything_under_what_it_matches() -> None:
    # The subtree goes with the MO the pattern matched, as it goes with one
    # named outright: expand leaves nothing for a stray child to hide in.
    assert compare(INTENDED[0], exclude="uni/tn-comm*", expand=True) == []


def test_a_pattern_matches_within_one_rn_and_not_across_a_slash() -> None:
    # "uni/*-bd1" is two RNs, so it is held against the tenants and matches
    # none. Matched against the text of a DN it would take a BD three RNs down
    # with it, and the comparison would go quiet about a BD nobody excluded.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"name": "bd2", "mtu": "1500"})],
    )
    reported = kinds(compare(tenant, INTENDED[1], exclude="uni/*-bd1", expand=True))
    assert reported == [
        ("extra", "uni/tn-demo/BD-bd1"),
        ("extra", "uni/tn-demo/BD-bd1/rsctx"),
    ]


def test_a_pattern_can_name_the_depth_it_matches_at() -> None:
    # The BDs go and the tenant above them stays, so the descr is still held to
    # the configuration.
    changed = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "descr": "changed"})
    (change,) = compare(changed, INTENDED[1], exclude="uni/tn-demo/BD-*")
    assert (change.kind, change.dn) == ("modified", "uni/tn-demo")
    assert change.attributes == {"descr": ("", "changed")}


def test_the_mos_going_with_a_subtree_are_counted_without_the_pattern_excluded_ones() -> None:
    # As for a DN named outright: the count is read off the pruned index, so a
    # tenant whose BDs are all excluded takes none of them with it.
    (pruned,) = compare(INTENDED[1], exclude="uni/tn-demo/BD-*")
    assert pruned.child_count == 0


def test_brackets_in_a_pattern_are_the_brackets_of_a_naming_value() -> None:
    # fnmatch would read "[dc]" as a set of characters and take both tenants.
    # Here it is text an RN would have to hold, and no RN does.
    reported = kinds(compare(INTENDED[0], exclude="uni/tn-[dc]*"))
    assert reported == [("extra", "uni/tn-common")]


def test_a_star_reaches_into_a_naming_value_that_holds_a_slash() -> None:
    imdata = [
        mo(
            "fvTenant",
            {"dn": "uni/tn-x", "name": "x"},
            [mo("fvBD", {"name": "b"}, [mo("fvSubnet", {"ip": "10.0.0.1/24"})])],
        )
    ]
    reported = kinds(
        compare(
            mo("fvTenant", {"dn": "uni/tn-x", "name": "x"}),
            imdata=imdata,
            expand=True,
            exclude="uni/tn-x/BD-b/subnet-*",
        )
    )
    assert reported == [("extra", "uni/tn-x/BD-b")]


def test_a_pattern_matching_nothing_is_accepted() -> None:
    assert kinds(compare(INTENDED[0], exclude="uni/tn-typo*")) == [("extra", "uni/tn-common")]


def test_a_pattern_and_a_dn_can_be_given_together() -> None:
    assert compare(*INTENDED, exclude=["uni/tn-demo", "uni/tn-comm*"]) == []


def test_a_star_on_its_own_excludes_the_whole_comparison() -> None:
    # It matches uni, which is the ancestor of everything, so this is the root
    # named a second way.
    assert compare(*INTENDED, exclude="*") == []


def test_a_pattern_is_read_with_or_without_its_slashes() -> None:
    assert compare(INTENDED[0], exclude="/uni/tn-comm*/") == []


def test_two_stars_are_refused() -> None:
    # Matching across RNs is the one thing a pattern here does not do. Read as
    # two "*" it would match nothing and quietly exclude nothing, so it is
    # refused rather than read at all.
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, exclude="uni/**/BD-bd1")
    assert '"**" is not supported' in str(exc.value)


def test_an_unidentified_mo_under_a_pattern_excluded_one_does_not_stop_the_comparison() -> None:
    # The exclusion reaches the merge side too: which fvBD the input meant is a
    # question about a subtree nothing will be reported about.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    assert compare(tenant, INTENDED[1], exclude="uni/tn-dem*") == []
