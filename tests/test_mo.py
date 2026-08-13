from __future__ import annotations

from a4i import mo

# What the APIC returns for a query-target=children GET: one MO per child, each
# carrying the dn the APIC wrote.
CHILDREN = [
    {"fvTenant": {"attributes": {"dn": "uni/tn-infra"}}},
    {"fvTenant": {"attributes": {"dn": "uni/tn-common"}}},
    {"infraInfra": {"attributes": {"dn": "uni/infra"}}},
]


def test_the_dns_come_back_sorted() -> None:
    # Sorted rather than in the order the APIC sent them, so that what gets
    # printed and what gets walked follow from the set of MOs alone.
    assert mo.top_level_dns(CHILDREN) == ["uni/infra", "uni/tn-common", "uni/tn-infra"]


def test_the_same_dn_twice_is_returned_once() -> None:
    twice = [
        {"fvTenant": {"attributes": {"dn": "uni/tn-demo"}}},
        {"fvTenant": {"attributes": {"dn": "uni/tn-demo"}}},
    ]
    assert mo.top_level_dns(twice) == ["uni/tn-demo"]


def test_a_leading_or_trailing_slash_is_dropped() -> None:
    assert mo.top_level_dns([{"fvTenant": {"attributes": {"dn": "/uni/tn-demo/"}}}]) == [
        "uni/tn-demo"
    ]


def test_only_the_top_level_is_read() -> None:
    """Nothing is built here, so a child the response nests is not named.

    This is the other half of child_dn, which does build a DN -- from the
    class's RN format, for an MO an input asked for. A response is not an
    input: what it does not name outright is left out rather than stood in for.
    """

    nested = [
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo"},
                "children": [{"fvBD": {"attributes": {"dn": "uni/tn-demo/BD-bd1"}}}],
            }
        }
    ]
    assert mo.top_level_dns(nested) == ["uni/tn-demo"]


def test_an_mo_without_a_dn_is_left_out() -> None:
    assert mo.top_level_dns([{"fvTenant": {"attributes": {"name": "demo"}}}]) == []
    assert mo.top_level_dns([{"fvTenant": {"attributes": {"dn": "/"}}}]) == []


def test_what_is_not_a_well_formed_mo_is_skipped() -> None:
    # One good MO among the malformed still comes back: a response that cannot
    # be read in full is not a response with nothing in it.
    assert mo.top_level_dns(
        [
            "not an MO",
            {},
            {"fvTenant": "not a body"},
            {"a": {}, "b": {}},
            {"fvTenant": {"attributes": {"dn": "uni/tn-demo"}}},
        ]
    ) == ["uni/tn-demo"]


def test_an_empty_or_absent_imdata_yields_nothing() -> None:
    # The callers pass data.get("imdata") straight through, so a response
    # without one must not raise.
    assert mo.top_level_dns([]) == []
    assert mo.top_level_dns(None) == []
    assert mo.top_level_dns({"imdata": []}) == []


# -- taking a DN apart -----------------------------------------------------


def test_a_dn_is_split_at_the_slashes_between_rns() -> None:
    assert mo.split_rns("uni/tn-demo/BD-bd1") == ["uni", "tn-demo", "BD-bd1"]


def test_a_slash_inside_a_naming_value_is_not_a_separator() -> None:
    assert mo.split_rns("uni/tn-x/BD-b/subnet-[10.0.0.1/24]") == [
        "uni",
        "tn-x",
        "BD-b",
        "subnet-[10.0.0.1/24]",
    ]


def test_a_dn_of_one_rn_is_one_rn() -> None:
    assert mo.split_rns("uni") == ["uni"]


def test_the_brackets_of_a_path_inside_a_path_are_counted() -> None:
    # A tDn inside an RN brings its own brackets, and the "/" in eth1/1 is
    # inside both of them.
    dn = "uni/tn-x/ap-a/epg-e/rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]"
    assert mo.split_rns(dn)[-1] == "rspathAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]"


# -- what a comparison leaves out ------------------------------------------


def test_a_name_without_a_star_covers_the_dn_and_its_subtree() -> None:
    excluded = mo.Exclusions(["uni/tn-demo"])
    assert excluded.covers("uni/tn-demo")
    assert excluded.covers("uni/tn-demo/BD-bd1/rsctx")
    assert not excluded.covers("uni/tn-demo2")


def test_a_pattern_covers_the_rns_it_matches_and_their_subtrees() -> None:
    excluded = mo.Exclusions(["uni/tn-test*"])
    assert excluded.covers("uni/tn-test")
    assert excluded.covers("uni/tn-testbed/BD-b")
    assert not excluded.covers("uni/tn-prod")


def test_a_pattern_of_two_rns_never_covers_an_mo_of_one() -> None:
    # The prefix read off the DN has to be as deep as the pattern, or there is
    # nothing to hold the pattern against.
    assert not mo.Exclusions(["uni/tn-*"]).covers("uni")


def test_a_star_does_not_reach_past_the_rn_it_is_written_in() -> None:
    assert not mo.Exclusions(["uni/*-bd1"]).covers("uni/tn-demo/BD-bd1")


def test_nothing_is_covered_when_nothing_is_excluded() -> None:
    assert not mo.Exclusions().covers("uni/tn-demo")
    assert not mo.Exclusions()
