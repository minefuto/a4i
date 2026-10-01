from __future__ import annotations

import pytest

from a4i import _diff as diff

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


# diff takes a single body and a list of MOs is one, so the arguments here are flattened
# into that list rather than passed as several inputs.
def compare(*mos, imdata: list | None = None, expand: bool = False, exclude=None) -> list:
    config = [one for arg in mos for one in (arg if isinstance(arg, list) else [arg])]
    return diff.compare(
        config,
        fabric=FABRIC if imdata is None else imdata,
        expand=expand,
        exclude=exclude,
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
    (change,) = diff.compare([tenant], fabric=imdata)
    assert change.attributes == {"descr": ("", None)}


def test_an_attribute_the_configuration_asks_for_and_the_fabric_lacks_is_reported() -> None:
    tenant = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "nameAlias": "prod"})
    imdata = [mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo"})]
    (change,) = diff.compare([tenant], fabric=imdata)
    assert change.attributes == {"nameAlias": (None, "prod")}


def test_identity_attributes_are_never_diffed() -> None:
    tenant = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "status": "created"})
    imdata = [
        mo("fvTenant", {"dn": "uni/tn-demo", "rn": "tn-demo", "name": "demo", "childAction": ""})
    ]
    assert diff.compare([tenant], fabric=imdata) == []


def test_attributes_are_reported_in_name_order() -> None:
    tenant = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "descr": "b"})
    imdata = [mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "zzz": "z", "aaa": "a"})]
    (change,) = diff.compare([tenant], fabric=imdata)
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
    # Reported under the DN ACI would give it, which no MO on the fabric had to
    # show.
    tenant = mo("fvTenant", {"dn": "uni/tn-new", "name": "new"}, [mo("fvCtx", {"name": "v1"})])
    changes = compare(*INTENDED, tenant, expand=True)
    assert kinds(changes) == [
        ("missing", "uni/tn-new"),
        ("missing", "uni/tn-new/ctx-v1"),
    ]


def test_two_mos_of_a_class_the_fabric_lacks_stay_two() -> None:
    # Both once landed on uni/tn-demo/? and the second overwrote the first: an MO
    # lost that way reads as a fabric that matches.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvCtx", {"name": "vrf-a"}), mo("fvCtx", {"name": "vrf-b"})],
    )
    reported = kinds(compare(tenant, INTENDED[1], expand=True))
    assert ("missing", "uni/tn-demo/ctx-vrf-a") in reported
    assert ("missing", "uni/tn-demo/ctx-vrf-b") in reported


def test_a_root_mo_of_a_class_the_fabric_lacks_is_reported_missing() -> None:
    changes = compare(*INTENDED, mo("physDomP", {"name": "d1"}), expand=True)
    assert kinds(changes) == [("missing", "uni/phys-d1")]


def test_a_root_mo_the_dictionary_says_hangs_elsewhere_is_refused() -> None:
    # vzBrCP hangs under fvTenant, so no body posted at uni could carry this one.
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, mo("vzBrCP", {"name": "c1"}))
    assert "uni/brc-c1" in str(exc.value)


# -- MOs with no bundled RN format -----------------------------------------


def test_a_class_the_dictionary_lacks_is_refused_without_a_dn_or_rn() -> None:
    # No RN it could be keyed by would ever match the DN the APIC reports for it.
    tenant = mo("fvTenant", {"dn": "uni/tn-new", "name": "new"}, [mo("fooBar", {"name": "b1"})])
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, tenant, expand=True)
    assert "fooBar under uni/tn-new (a class the bundled dictionary lacks)" in str(exc.value)


# -- MOs the input does not name -------------------------------------------


def test_an_mo_the_input_does_not_identify_is_refused() -> None:
    # An fvBD RN is "BD-{name}" and this one gives no name, so it could be bd1 or
    # bd2. Reporting it missing while reporting the real one extra would be worse.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    with pytest.raises(ValueError) as exc:
        compare(tenant, INTENDED[1])
    assert "fvBD under uni/tn-demo" in str(exc.value)
    assert "BD-{name}" in str(exc.value)
    assert '"dn", an "rn"' in str(exc.value)


def test_it_is_refused_even_where_the_fabric_has_no_such_mo() -> None:
    # Refused without the fabric being consulted: there is no MO this could be
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
    # The class says it is the wrapper, so a body leaving the dn to the URL reads
    # the same way.
    assert compare(mo("polUni", {}, INTENDED)) == []


def test_a_malformed_input_is_refused_rather_than_skipped() -> None:
    # Skipping it would report the fabric extra for carrying what the element was
    # meant to describe.
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, "not an mo", {"a": {}, "b": {}})
    assert "[2]" in str(exc.value)
    assert "[3]" in str(exc.value)


# -- reading a response back as the configuration it describes -------------
#
# Feeding a "config-only" response straight back has to report nothing, which it
# only does if both sides work a DN out the same way -- and the APIC does not
# always spell one out on a child.


def test_a_response_compared_against_itself_shows_no_differences() -> None:
    assert diff.compare([mo("polUni", {"dn": "uni"}, FABRIC)], fabric=FABRIC) == []


def test_a_child_named_only_by_an_rn_is_read_the_same_on_both_sides() -> None:
    imdata = [
        mo(
            "fvTenant",
            {"dn": "uni/tn-demo", "name": "demo"},
            [mo("fvBD", {"rn": "BD-bd1", "name": "bd1"})],
        )
    ]
    assert diff.compare([mo("polUni", {"dn": "uni"}, imdata)], fabric=imdata) == []


def test_a_child_named_by_neither_a_dn_nor_an_rn_is_kept_on_the_fabric_side() -> None:
    # The RN format has to be applied to the response as well: dropping the branch
    # would report the whole subtree missing from a fabric carrying it.
    imdata = [
        mo(
            "fvTenant",
            {"dn": "uni/tn-demo", "name": "demo"},
            [mo("fvAp", {"name": "ap1"}, [mo("fvAEPg", {"name": "epg1"})])],
        )
    ]
    assert diff.compare([mo("polUni", {"dn": "uni"}, imdata)], fabric=imdata) == []


# -- MOs left out of the comparison ----------------------------------------


def test_an_excluded_mo_the_fabric_has_is_not_reported_extra() -> None:
    # tn-common is left out of the configuration, and excluded as well.
    assert compare(INTENDED[0], exclude="uni/tn-common") == []


def test_excluding_an_mo_excludes_everything_under_it() -> None:
    # With expand there is nothing to summarise a subtree into, so this is where a
    # child that outlived its parent would show.
    assert compare(INTENDED[0], exclude="uni/tn-common", expand=True) == []


def test_an_excluded_mo_the_configuration_asks_for_is_not_reported_missing() -> None:
    # The other side of the same rule: an excluded MO is not missing either,
    # however loudly the input asks for it.
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
    # Excluding BD-bd1 takes the rsctx under it along, leaving one MO of the three
    # to go with the tenant.
    (whole,) = compare(INTENDED[1])
    assert whole.child_count == 3
    (pruned,) = compare(INTENDED[1], exclude="uni/tn-demo/BD-bd1")
    assert pruned.child_count == 1


def test_a_dn_that_is_not_an_rn_boundary_excludes_nothing() -> None:
    # A prefix of the text of uni/tn-common and the DN of no MO.
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
    # A subnet's RN holds a "/" of its own, so this text is an ancestor of nothing
    # once the DN is split properly.
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
    # Excluding what is not there hides no difference, and the same command line
    # can serve a fabric that has the tenant and one that does not.
    assert kinds(compare(INTENDED[0], exclude="uni/tn-typo")) == [("extra", "uni/tn-common")]


def test_one_dn_can_be_given_as_a_string_or_as_a_sequence() -> None:
    # A string is one DN, never a list to split: an ACI naming value can hold a
    # comma.
    assert compare(INTENDED[0], exclude=["uni/tn-common"]) == []
    assert compare(INTENDED[0], exclude=("uni/tn-common",)) == []


def test_several_dns_are_excluded_at_once() -> None:
    assert compare(*INTENDED, exclude=["uni/tn-demo", "uni/tn-common"]) == []


def test_a_dn_is_read_with_or_without_its_slashes() -> None:
    # As a DN is read anywhere else: a leading or trailing "/" no longer marks
    # anything.
    assert compare(INTENDED[0], exclude="/uni/tn-common/") == []


def test_excluding_the_root_excludes_the_whole_comparison() -> None:
    # Nothing special is made of uni, which is the ancestor of every MO.
    assert compare(*INTENDED, exclude="uni") == []


def test_an_empty_dn_is_refused() -> None:
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, exclude=" / ")
    assert "cannot be empty" in str(exc.value)


def test_an_unidentified_mo_under_an_excluded_one_does_not_stop_the_comparison() -> None:
    # Which fvBD the input meant is a question about a subtree nothing will be
    # reported about.
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
    # The input names no one fvBD, so it could be the excluded one or another.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    with pytest.raises(ValueError):
        compare(tenant, INTENDED[1], exclude="uni/tn-demo/BD-bd1")


# -- MOs left out by a pattern ---------------------------------------------

# Three tenants whose names start alike, which is what a pattern is for and what
# it gets wrong. The fabric has all three; the configuration describes prod.
TENANTS = [
    mo("fvTenant", {"dn": "uni/tn-test", "name": "test"}),
    mo("fvTenant", {"dn": "uni/tn-testbed", "name": "testbed"}),
    mo("fvTenant", {"dn": "uni/tn-prod", "name": "prod"}),
]
PROD = mo("fvTenant", {"dn": "uni/tn-prod", "name": "prod"})


def test_a_pattern_excludes_every_mo_whose_rn_it_matches() -> None:
    assert compare(PROD, imdata=TENANTS, exclude="uni/tn-test*") == []


def test_a_star_stands_for_nothing_as_well_as_for_something() -> None:
    # A pattern that happens to end in a "*" and a name written out in full leave
    # out the same MO.
    reported = kinds(compare(PROD, imdata=TENANTS, exclude="uni/tn-testbed*"))
    assert reported == [("extra", "uni/tn-test")]


def test_an_mo_the_pattern_does_not_match_is_still_compared() -> None:
    changed = mo("fvTenant", {"dn": "uni/tn-prod", "name": "prod", "descr": "changed"})
    (change,) = compare(changed, imdata=TENANTS, exclude="uni/tn-test*")
    assert (change.kind, change.dn) == ("modified", "uni/tn-prod")


def test_a_pattern_excludes_everything_under_what_it_matches() -> None:
    # The subtree goes with the MO the pattern matched, and expand leaves nothing
    # for a stray child to hide in.
    assert compare(INTENDED[0], exclude="uni/tn-comm*", expand=True) == []


def test_a_pattern_matches_within_one_rn_and_not_across_a_slash() -> None:
    # "uni/*-bd1" is two RNs, so it is held against the tenants and matches none.
    # Against the text of a DN it would quietly take a BD three RNs down with it.
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
    # The BDs go and the tenant above them stays.
    changed = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "descr": "changed"})
    (change,) = compare(changed, INTENDED[1], exclude="uni/tn-demo/BD-*")
    assert (change.kind, change.dn) == ("modified", "uni/tn-demo")
    assert change.attributes == {"descr": ("", "changed")}


def test_the_mos_going_with_a_subtree_are_counted_without_the_pattern_excluded_ones() -> None:
    # The count is read off the pruned index, so a tenant whose BDs are all
    # excluded takes none of them with it.
    (pruned,) = compare(INTENDED[1], exclude="uni/tn-demo/BD-*")
    assert pruned.child_count == 0


def test_brackets_in_a_pattern_are_the_brackets_of_a_naming_value() -> None:
    # fnmatch would read "[dc]" as a set of characters and take both tenants; here
    # it is text an RN would have to hold, and no RN does.
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
    # It matches uni, the ancestor of everything.
    assert compare(*INTENDED, exclude="*") == []


def test_a_pattern_is_read_with_or_without_its_slashes() -> None:
    assert compare(INTENDED[0], exclude="/uni/tn-comm*/") == []


def test_two_stars_are_refused() -> None:
    # Read as two "*" it would match nothing and quietly exclude nothing, so it is
    # refused rather than read at all.
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, exclude="uni/**/BD-bd1")
    assert '"**" is not supported' in str(exc.value)


def test_an_unidentified_mo_under_a_pattern_excluded_one_does_not_stop_the_comparison() -> None:
    # The exclusion reaches the merge side too.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    assert compare(tenant, INTENDED[1], exclude="uni/tn-dem*") == []


# -- MOs an exception brings back ------------------------------------------

# tn-demo as the fabric has it, with one attribute changed: an exception is only
# shown to work by a difference reported through it.
CHANGED_DEMO = mo(
    "fvTenant",
    {"dn": "uni/tn-demo", "name": "demo", "descr": "changed"},
    [
        mo("fvBD", {"name": "bd1", "mtu": "1500"}, [mo("fvRsCtx", {"tnFvCtxName": "v1"})]),
        mo("fvBD", {"name": "bd2", "mtu": "1500"}),
    ],
)


def test_an_exception_leaves_the_mo_it_names_in_the_comparison() -> None:
    # Every tenant is excluded and one named back in.
    reported = kinds(compare(CHANGED_DEMO, exclude=["uni/tn-*", "!uni/tn-demo"]))
    assert reported == [("modified", "uni/tn-demo")]


def test_an_exclusion_deeper_than_an_exception_is_the_one_that_counts() -> None:
    # The tenant comes back and its BDs go: the only way to compare a tenant's own
    # attributes and nothing under it.
    tenant = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo", "descr": "changed"})
    reported = kinds(
        compare(tenant, exclude=["uni/tn-*", "!uni/tn-demo", "uni/tn-demo/BD-*"], expand=True)
    )
    assert reported == [("modified", "uni/tn-demo")]


def test_an_exception_is_read_with_or_without_its_spaces_and_slashes() -> None:
    reported = kinds(compare(CHANGED_DEMO, exclude=["uni/tn-*", " ! /uni/tn-demo/ "]))
    assert reported == [("modified", "uni/tn-demo")]


def test_exceptions_on_their_own_are_refused() -> None:
    # They exclude nothing, so a name written this way and nothing else is a
    # missing exclusion rather than a request.
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, exclude="!uni/tn-common")
    assert "exceptions alone exclude nothing" in str(exc.value)


def test_an_exception_naming_nothing_is_refused() -> None:
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, exclude=["uni/tn-*", "!"])
    assert "cannot be empty" in str(exc.value)


def test_two_stars_are_refused_in_an_exception_too() -> None:
    with pytest.raises(ValueError) as exc:
        compare(*INTENDED, exclude=["uni/tn-*", "!uni/**/BD-bd1"])
    assert '"**" is not supported' in str(exc.value)


def test_an_unidentified_mo_an_exception_brought_back_is_still_refused() -> None:
    # The exception reaches the merge side as the exclusion does: the subtree is
    # compared again, so the input has to name this MO after all.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [mo("fvBD", {"mtu": "9000"})],
    )
    with pytest.raises(ValueError) as exc:
        compare(tenant, exclude=["uni/tn-*", "!uni/tn-demo"])
    assert "fvBD under uni/tn-demo" in str(exc.value)


# -- MOs left out by an attribute condition ---------------------------------

# Two tenants a DN tells apart no better than a pattern does: what separates them
# is what an attribute says.
MARKED = [
    mo(
        "fvTenant",
        {"dn": "uni/tn-a", "name": "a", "descr": "auto-generated"},
        [mo("fvBD", {"dn": "uni/tn-a/BD-b", "name": "b"})],
    ),
    mo("fvTenant", {"dn": "uni/tn-b", "name": "b", "descr": "by hand"}),
]
BY_HAND = mo("fvTenant", {"dn": "uni/tn-b", "name": "b", "descr": "by hand"})


def test_a_condition_excludes_the_mos_whose_attribute_matches() -> None:
    assert compare(BY_HAND, imdata=MARKED, exclude="uni/tn-*[descr=auto-*]") == []


def test_a_condition_excludes_everything_under_what_it_matched() -> None:
    # The BD hangs under the matched tenant and is left out with it.
    assert compare(BY_HAND, imdata=MARKED, exclude="uni/tn-*[descr=auto-*]", expand=True) == []


def test_an_mo_the_condition_does_not_match_is_still_compared() -> None:
    reported = kinds(compare(BY_HAND, imdata=MARKED, exclude="uni/tn-*[descr=by*]"))
    assert reported == [("extra", "uni/tn-a")]


def test_the_value_is_matched_in_full_rather_than_anywhere_in_it() -> None:
    # "auto" is not the whole of "auto-generated", so nothing is left out.
    reported = kinds(compare(BY_HAND, imdata=MARKED, exclude="uni/tn-*[descr=auto]"))
    assert reported == [("extra", "uni/tn-a")]


def test_the_fabric_value_alone_is_enough_to_leave_an_mo_out() -> None:
    # The configuration calls it something else, so a condition read off the
    # intended side alone would report the fabric's value modified -- an exclusion
    # inventing the difference it was written to quiet.
    intended = mo("fvTenant", {"dn": "uni/tn-a", "name": "a", "descr": "by hand"})
    assert compare(intended, BY_HAND, imdata=MARKED, exclude="uni/tn-*[descr=auto-*]") == []


def test_the_intended_value_alone_is_enough_to_leave_an_mo_out() -> None:
    # Not on the fabric, so without the condition it is missing; the
    # configuration's own descr is what matches it.
    wanted = mo("fvTenant", {"dn": "uni/tn-c", "name": "c", "descr": "auto-made"})
    assert compare(BY_HAND, wanted, imdata=MARKED, exclude="uni/tn-*[descr=auto-*]") == []


def test_an_mo_without_the_attribute_is_not_left_out() -> None:
    # tn-b carries no "annotation" at all, and a condition asks what a value is.
    reported = kinds(compare(BY_HAND, imdata=MARKED, exclude="uni/tn-*[annotation=*]"))
    assert reported == [("extra", "uni/tn-a")]


def test_a_condition_matches_at_its_own_depth_alone() -> None:
    # The BD is one deeper than the pattern, so it is left out because the tenant
    # above it was.
    reported = kinds(compare(BY_HAND, imdata=MARKED, exclude="uni/tn-*[name=b]", expand=True))
    assert reported == [("extra", "uni/tn-a"), ("extra", "uni/tn-a/BD-b")]


def test_a_dn_ending_in_a_bracket_is_not_read_as_a_condition() -> None:
    # A naming value holds no "=", so this ends in a "]" and is a DN, brackets and
    # all.
    subnet = [
        mo(
            "fvTenant",
            {"dn": "uni/tn-x", "name": "x"},
            [
                mo(
                    "fvBD",
                    {"dn": "uni/tn-x/BD-b", "name": "b"},
                    [
                        mo(
                            "fvSubnet",
                            {"dn": "uni/tn-x/BD-b/subnet-[10.0.0.1/24]", "ip": "10.0.0.1/24"},
                        )
                    ],
                )
            ],
        )
    ]
    config = mo(
        "fvTenant",
        {"dn": "uni/tn-x", "name": "x"},
        [mo("fvBD", {"name": "b"})],
    )
    assert compare(config, imdata=subnet, exclude="uni/tn-x/BD-b/subnet-[10.0.0.1/24]") == []


def test_a_dn_ending_in_a_bracket_can_still_carry_a_condition() -> None:
    subnet = [
        mo(
            "fvTenant",
            {"dn": "uni/tn-x", "name": "x"},
            [
                mo(
                    "fvBD",
                    {"dn": "uni/tn-x/BD-b", "name": "b"},
                    [
                        mo(
                            "fvSubnet",
                            {
                                "dn": "uni/tn-x/BD-b/subnet-[10.0.0.1/24]",
                                "ip": "10.0.0.1/24",
                                "descr": "auto-made",
                            },
                        )
                    ],
                )
            ],
        )
    ]
    config = mo(
        "fvTenant",
        {"dn": "uni/tn-x", "name": "x"},
        [mo("fvBD", {"name": "b"})],
    )
    left_out = "uni/tn-x/BD-b/subnet-[10.0.0.1/24][descr=auto-*]"
    assert compare(config, imdata=subnet, exclude=left_out) == []


def test_an_exception_may_carry_a_condition_of_its_own() -> None:
    # Every tenant is left out, but the one the fabric marked by hand comes back --
    # and it differs.
    changed = mo("fvTenant", {"dn": "uni/tn-b", "name": "b", "descr": "changed"})
    (change,) = compare(changed, imdata=MARKED, exclude=["uni/tn-*", "!uni/tn-*[descr=by*]"])
    assert (change.kind, change.dn) == ("modified", "uni/tn-b")


def test_a_condition_naming_no_attribute_is_refused() -> None:
    with pytest.raises(ValueError) as exc:
        compare(BY_HAND, imdata=MARKED, exclude="uni/tn-*[=auto-*]")
    assert "names an attribute" in str(exc.value)


def test_a_condition_does_not_excuse_an_unidentified_mo_under_it() -> None:
    # A condition is settled once the MOs are indexed, which is after the input
    # had to name this one.
    tenant = mo(
        "fvTenant",
        {"dn": "uni/tn-a", "name": "a", "descr": "auto-generated"},
        [mo("fvBD", {"mtu": "9000"})],
    )
    with pytest.raises(ValueError) as exc:
        compare(tenant, imdata=MARKED, exclude="uni/tn-*[descr=auto-*]")
    assert "fvBD under uni/tn-a" in str(exc.value)


def test_str_of_the_result_is_the_report() -> None:
    assert str(diff.compare(INTENDED, fabric=FABRIC)) == "no differences"
    report = str(diff.compare(INTENDED, fabric=FABRIC[:1]))
    assert report == (
        '+ fvTenant uni/tn-common  (missing: 1 child MO)\n  + name: "common"\n\n'
        "1 missing, 0 modified, 0 extra"
    )
