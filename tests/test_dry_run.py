from __future__ import annotations

import pytest

from a4i import _dry_run as dry_run
from a4i import _mo as mo_
from a4i._merge import read

# -- the current tree the APIC would return for /uni/tn-demo ---------------


def mo(class_name: str, attributes: dict, children: list | None = None) -> dict:
    body: dict = {"attributes": attributes}
    if children is not None:
        body["children"] = children
    return {class_name: body}


TENANT = [
    mo(
        "fvTenant",
        {"dn": "uni/tn-demo", "name": "demo", "descr": ""},
        [
            mo(
                "fvBD",
                {"dn": "uni/tn-demo/BD-bd1", "name": "bd1", "arpFlood": "no"},
                [
                    mo("fvRsCtx", {"dn": "uni/tn-demo/BD-bd1/rsctx", "tnFvCtxName": "v1"}),
                    mo(
                        "fvSubnet",
                        {"dn": "uni/tn-demo/BD-bd1/subnet-[10.0.0.1/24]", "ip": "10.0.0.1/24"},
                    ),
                ],
            ),
            mo("fvBD", {"dn": "uni/tn-demo/BD-bd2", "name": "bd2"}),
            mo("fvAp", {"dn": "uni/tn-demo/ap-a", "name": "a"}),
        ],
    )
]


# Both sides go through a4i._merge.read, as Client.dry_run runs it, which is the point:
# what the body means and what the response means are settled by the same reading.
def dry_run_compare(
    body: object, imdata: list | None = None, dn: str = "uni/tn-demo"
) -> list[dry_run.Change]:
    intended = read(dry_run.rooted(dn, "mo", body))
    current = read(TENANT if imdata is None else imdata, loose=True)
    return dry_run.compare(intended, current)


# -- root_dn ---------------------------------------------------------------


def test_root_dn_uses_an_mo_target() -> None:
    assert dry_run.root_dn("uni/tn-demo", "mo", mo("fvTenant", {"name": "demo"})) == "uni/tn-demo"
    # A leading "/" no longer marks anything, so it is dropped like a trailing one.
    assert dry_run.root_dn("/uni/tn-demo", "mo", mo("fvTenant", {"name": "demo"})) == "uni/tn-demo"


def test_root_dn_prefers_the_dn_attribute_over_the_target() -> None:
    # Posting to a parent with the DN written in the body is idiomatic ACI.
    body = mo("fvTenant", {"dn": "uni/tn-demo", "name": "demo"})
    assert dry_run.root_dn("uni", "mo", body) == "uni/tn-demo"
    # It wins over a class target too, which names no place of its own.
    assert dry_run.root_dn("fvTenant", "class", body) == "uni/tn-demo"


@pytest.mark.parametrize(
    "target_kind_and_body",
    [
        # A class target says what the body is, not where it goes.
        ("fvTenant", "class", mo("fvTenant", {"name": "demo"})),
        ("uni/tn-demo", "class", mo("fvTenant", {"name": "demo"})),
        # Not an MO at all.
        ("uni/tn-demo", "mo", {"fvTenant": {}, "fvBD": {}}),
        ("uni/tn-demo", "mo", ["not an mo"]),
        ("/", "mo", mo("fvTenant", {"name": "demo"})),
    ],
)
def test_root_dn_gives_up_when_the_mo_is_unidentified(target_kind_and_body) -> None:
    target, kind, body = target_kind_and_body
    assert dry_run.root_dn(target, kind, body) is None


# -- attribute diff --------------------------------------------------------


def test_only_changed_attributes_are_reported() -> None:
    body = mo("fvTenant", {"name": "demo", "descr": "prod"})
    (change,) = dry_run_compare(body)
    assert change.kind == "modified"
    assert change.dn == "uni/tn-demo"
    # name is set to the value it already has, so it is not a change.
    assert change.attributes == {"descr": ("", "prod")}


def test_an_identical_body_is_no_change_at_all() -> None:
    assert dry_run_compare(mo("fvTenant", {"name": "demo", "descr": ""})) == []


def test_an_attribute_absent_from_the_response_reads_as_newly_set() -> None:
    (change,) = dry_run_compare(mo("fvTenant", {"nameAlias": "prod"}))
    assert change.attributes == {"nameAlias": (None, "prod")}


def test_identity_attributes_are_not_diffed() -> None:
    # dn/rn/status describe the request, not the configuration.
    body = mo("fvTenant", {"dn": "uni/tn-demo", "rn": "tn-demo", "status": "modified"})
    assert dry_run_compare(body) == []


def test_a_missing_mo_is_created_with_every_attribute_it_carries() -> None:
    body = mo("fvTenant", {"name": "new", "descr": "x"})
    (change,) = dry_run_compare(body, imdata=[], dn="uni/tn-new")
    assert change.kind == "created"
    assert change.dn == "uni/tn-new"
    assert change.attributes == {"name": (None, "new"), "descr": (None, "x")}


# -- resolving children ----------------------------------------------------


def test_an_existing_child_is_matched_by_its_naming_property() -> None:
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1", "arpFlood": "yes"})])
    (change,) = dry_run_compare(body)
    assert (change.kind, change.dn) == ("modified", "uni/tn-demo/BD-bd1")
    assert change.attributes == {"arpFlood": ("no", "yes")}


def test_a_new_child_gets_the_dn_its_rn_format_predicts() -> None:
    # The bundled rnFormat for fvBD is "BD-{name}".
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd3"})])
    (change,) = dry_run_compare(body)
    assert (change.kind, change.dn) == ("created", "uni/tn-demo/BD-bd3")


def test_a_child_of_a_class_the_fabric_lacks_gets_a_real_dn_too() -> None:
    # No fvCtx exists here, but the RN format does not come from the fabric.
    body = mo("fvTenant", {"name": "demo"}, [mo("fvCtx", {"name": "v1"})])
    (change,) = dry_run_compare(body)
    assert (change.kind, change.dn) == ("created", "uni/tn-demo/ctx-v1")


def test_a_relation_with_a_fixed_rn_is_matched_as_the_only_one() -> None:
    # fvRsCtx puts no attribute value in its RN, and a BD has exactly one.
    body = mo(
        "fvTenant",
        {"name": "demo"},
        [mo("fvBD", {"name": "bd1"}, [mo("fvRsCtx", {"tnFvCtxName": "v2"})])],
    )
    (change,) = dry_run_compare(body)
    assert (change.kind, change.dn) == ("modified", "uni/tn-demo/BD-bd1/rsctx")
    assert change.attributes == {"tnFvCtxName": ("v1", "v2")}


def test_a_bracketed_naming_value_is_matched() -> None:
    body = mo(
        "fvTenant",
        {"name": "demo"},
        [mo("fvBD", {"name": "bd1"}, [mo("fvSubnet", {"ip": "10.0.0.1/24", "scope": "public"})])],
    )
    (change,) = dry_run_compare(body)
    assert change.dn == "uni/tn-demo/BD-bd1/subnet-[10.0.0.1/24]"
    assert change.attributes == {"scope": (None, "public")}


def test_a_new_bracketed_child_keeps_the_bracket_in_its_predicted_dn() -> None:
    body = mo(
        "fvTenant",
        {"name": "demo"},
        [mo("fvBD", {"name": "bd1"}, [mo("fvSubnet", {"ip": "10.0.1.1/24"})])],
    )
    (change,) = dry_run_compare(body)
    assert (change.kind, change.dn) == ("created", "uni/tn-demo/BD-bd1/subnet-[10.0.1.1/24]")


def test_an_explicit_child_dn_wins_over_every_guess() -> None:
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"dn": "uni/tn-demo/BD-bd2"})])
    assert dry_run_compare(body) == []


def test_an_explicit_child_rn_is_appended_to_the_parent() -> None:
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"rn": "BD-bd1", "arpFlood": "yes"})])
    (change,) = dry_run_compare(body)
    assert change.dn == "uni/tn-demo/BD-bd1"


def test_children_of_a_new_mo_are_new_too() -> None:
    body = mo("fvTenant", {"name": "new"}, [mo("fvBD", {"name": "bd1"})])
    tenant, bd = dry_run_compare(body, imdata=[], dn="uni/tn-new")
    assert (tenant.kind, tenant.dn) == ("created", "uni/tn-new")
    assert (bd.kind, bd.dn) == ("created", "uni/tn-new/BD-bd1")


# -- status ----------------------------------------------------------------


def test_a_deleted_mo_counts_the_children_that_go_with_it() -> None:
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1", "status": "deleted"})])
    (change,) = dry_run_compare(body)
    assert change.kind == "deleted"
    assert change.dn == "uni/tn-demo/BD-bd1"
    # fvRsCtx and fvSubnet go with it.
    assert change.child_count == 2


def test_a_deleted_mo_does_not_report_its_bodys_children() -> None:
    # Whatever the body says about the subtree, the subtree is going away.
    body = mo(
        "fvTenant",
        {"name": "demo"},
        [mo("fvBD", {"name": "bd1", "status": "deleted"}, [mo("fvRsCtx", {"tnFvCtxName": "v9"})])],
    )
    (change,) = dry_run_compare(body)
    assert change.kind == "deleted"


def test_deleting_an_mo_that_is_not_there_changes_nothing() -> None:
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "gone", "status": "deleted"})])
    assert dry_run_compare(body) == []


def test_created_on_an_existing_mo_is_a_warning() -> None:
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd1", "status": "created"})])
    (change,) = dry_run_compare(body)
    assert change.kind == "warning"
    assert change.dn == "uni/tn-demo/BD-bd1"
    assert "already exists" in change.message


def test_created_on_a_new_mo_is_just_a_creation() -> None:
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd9", "status": "created"})])
    (change,) = dry_run_compare(body)
    assert change.kind == "created"


def test_created_modified_on_an_existing_mo_is_not_a_warning() -> None:
    # "created,modified" is the usual create-or-update spelled out, which the APIC
    # takes whether the MO is there or not.
    body = mo(
        "fvTenant",
        {"name": "demo"},
        [mo("fvBD", {"name": "bd1", "arpFlood": "yes", "status": "created,modified"})],
    )
    (change,) = dry_run_compare(body)
    assert change.kind == "modified"


def test_modified_on_a_missing_mo_is_a_warning() -> None:
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd9", "status": "modified"})])
    warning, created = dry_run_compare(body)
    assert warning.kind == "warning"
    assert warning.dn == "uni/tn-demo/BD-bd9"
    assert "does not exist" in warning.message
    # The POST will fail, but what it meant to write is still worth showing.
    assert created.kind == "created"


def test_created_modified_on_a_missing_mo_is_just_a_creation() -> None:
    body = mo(
        "fvTenant", {"name": "demo"}, [mo("fvBD", {"name": "bd9", "status": "created,modified"})]
    )
    (change,) = dry_run_compare(body)
    assert change.kind == "created"


# An fvBD RN is "BD-{name}" and this one gives no name, so the MO it meant may well be
# on the fabric under another DN: reporting a made-up one as created while the real MO
# sits there unmentioned is worse.
def test_a_body_that_names_no_mo_is_refused_rather_than_compared() -> None:
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"mtu": "9000"})])
    with pytest.raises(ValueError) as exc:
        dry_run_compare(body)
    assert "fvBD" in str(exc.value)


def test_deleting_an_mo_the_body_does_not_name_is_refused_as_well() -> None:
    # The RN is no more buildable for a delete than for a create.
    body = mo("fvTenant", {"name": "demo"}, [mo("fvBD", {"mtu": "9000", "status": "deleted"})])
    with pytest.raises(ValueError):
        dry_run_compare(body)


def test_a_status_list_is_read_token_by_token() -> None:
    # Spacing and order are the writer's business; only the tokens count.
    child = mo("fvBD", {"name": "bd1", "status": " modified , deleted "})
    kinds = [change.kind for change in dry_run_compare(mo("fvTenant", {"name": "demo"}, [child]))]
    assert kinds == ["deleted"]


# -- RN splitting ----------------------------------------------------------


@pytest.mark.parametrize(
    ("dn", "rn"),
    [
        ("uni/tn-demo", "tn-demo"),
        ("uni/tn-demo/BD-bd1/subnet-[10.0.0.1/24]", "subnet-[10.0.0.1/24]"),
        ("uni", "uni"),
        (
            "uni/tn-a/out-o/lnodep-n/rspathL3OutAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]",
            "rspathL3OutAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]",
        ),
    ],
)
def test_tail_rn_ignores_slashes_inside_brackets(dn, rn) -> None:
    assert mo_.tail_rn(dn) == rn


# -- odds and ends ---------------------------------------------------------


def test_the_dn_the_apic_echoed_back_wins_over_the_one_we_asked_for() -> None:
    # A trailing slash or an odd target must not desynchronise the two trees.
    (change,) = dry_run_compare(mo("fvTenant", {"descr": "x"}), dn="uni/tn-demo/")
    assert change.dn == "uni/tn-demo"


def test_a_malformed_body_is_refused() -> None:
    with pytest.raises(ValueError):
        dry_run_compare("not an mo")


def test_a_malformed_child_is_refused_rather_than_skipped() -> None:
    body = mo("fvTenant", {"name": "demo"}, ["junk", mo("fvBD", {"name": "bd9"})])
    with pytest.raises(ValueError) as exc:
        dry_run_compare(body)
    assert "not an MO" in str(exc.value)


# -- a body below what it describes ------------------------------------------


def test_an_ancestor_the_body_does_not_describe_is_taken_as_there() -> None:
    body = mo("fvBD", {"dn": "uni/tn-demo/BD-bd1", "arpFlood": "yes"})
    changes = dry_run.check("uni/tn-demo/BD-bd1", body, kind="mo", fabric=TENANT)
    assert [(c.kind, c.dn) for c in changes] == [("modified", "uni/tn-demo/BD-bd1")]


def test_an_ancestor_the_body_does_not_describe_is_a_warning_when_absent() -> None:
    body = mo("fvBD", {"dn": "uni/tn-other/BD-bd1"})
    changes = dry_run.check("uni/tn-other/BD-bd1", body, kind="mo", fabric=TENANT)
    assert [(c.kind, c.dn) for c in changes] == [
        ("created", "uni/tn-other/BD-bd1"),
        ("warning", "uni/tn-other"),
    ]


def test_str_of_the_result_is_the_report() -> None:
    body = {
        "polUni": {"attributes": {}, "children": [{"fvTenant": {"attributes": {"name": "demo"}}}]}
    }
    assert str(dry_run.check("uni", body, kind="mo", fabric=[])) == (
        '+ fvTenant uni/tn-demo\n  + name: "demo"\n\n1 created, 0 modified, 0 deleted'
    )
