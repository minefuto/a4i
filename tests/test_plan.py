"""Narrowing a merged body to the MOs a POST of it would change."""

from __future__ import annotations

import pytest

from a4i import plan
from a4i.dry_run import compare
from a4i.merge import merge
from a4i.mo import Change

CURRENT = [
    {
        "fvTenant": {
            "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "dev"},
            "children": [
                {"fvBD": {"attributes": {"dn": "uni/tn-demo/BD-bd1", "name": "bd1", "mtu": "1500"}}}
            ],
        }
    }
]


def _plan(config, imdata=CURRENT) -> plan.Plan:
    """Merge a configuration, compare it against a fabric, and narrow it."""

    merged = merge(config)
    changes = compare(merged["polUni"]["children"][0], imdata, "uni/tn-demo")
    return plan.Plan(plan.body(merged, changes), changes)


def _children(built) -> list:
    return built["polUni"]["children"]


# -- what a plan carries ----------------------------------------------------


def test_a_modified_mo_carries_only_the_attributes_that_change() -> None:
    built = _plan(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "prod"},
            }
        }
    ).body
    # "name" is in the configuration and unchanged, so posting it back would
    # hand the fabric its own value.
    assert _children(built) == [
        {"fvTenant": {"attributes": {"rn": "tn-demo", "status": "modified", "descr": "prod"}}}
    ]


def test_a_created_mo_carries_every_attribute_the_configuration_gives() -> None:
    built = _plan(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "dev"},
                "children": [{"fvBD": {"attributes": {"name": "bd2", "mtu": "9000"}}}],
            }
        }
    ).body
    tenant = _children(built)[0]["fvTenant"]
    assert tenant["children"] == [
        {
            "fvBD": {
                "attributes": {"rn": "BD-bd2", "status": "created", "name": "bd2", "mtu": "9000"}
            }
        }
    ]


def test_an_mo_that_does_not_change_is_left_out() -> None:
    built = _plan(
        {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "dev"}}}
    ).body
    assert _children(built) == []


def test_an_unchanged_ancestor_is_carried_as_a_container() -> None:
    plan_ = _plan(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "dev"},
                "children": [{"fvBD": {"attributes": {"name": "bd1", "mtu": "9000"}}}],
            }
        }
    )
    tenant = _children(plan_.body)[0]["fvTenant"]
    # It is on the fabric already -- the comparison found it -- so it says so,
    # and a fabric without it refuses the POST rather than growing an empty one.
    assert tenant["attributes"] == {"rn": "tn-demo", "status": "modified"}
    assert tenant["children"][0]["fvBD"]["attributes"]["mtu"] == "9000"
    assert [(c.kind, c.dn) for c in plan_.changes] == [("modified", "uni/tn-demo/BD-bd1")]
    assert plan_.containers == 1


def test_a_deleted_mo_carries_its_status_and_none_of_its_subtree() -> None:
    built = _plan(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "dev"},
                "children": [
                    {"fvBD": {"attributes": {"name": "bd1", "status": "deleted", "mtu": "9000"}}}
                ],
            }
        }
    ).body
    bd = _children(built)[0]["fvTenant"]["children"][0]["fvBD"]
    assert bd == {"attributes": {"rn": "BD-bd1", "status": "deleted"}}


def test_the_status_the_configuration_wrote_is_not_carried_over() -> None:
    # "created,modified" is how a configuration says "either way". A plan is
    # about one POST against one fabric just read, and says which it found.
    built = _plan(
        {
            "fvTenant": {
                "attributes": {
                    "dn": "uni/tn-demo",
                    "name": "demo",
                    "descr": "prod",
                    "status": "created,modified",
                }
            }
        }
    ).body
    assert _children(built)[0]["fvTenant"]["attributes"]["status"] == "modified"


def test_a_plan_is_shaped_as_a_merged_body_is() -> None:
    built = _plan(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "dev"},
                "children": [
                    {"fvBD": {"attributes": {"name": "b"}}},
                    {"fvBD": {"attributes": {"name": "a"}}},
                ],
            }
        }
    ).body
    assert built["polUni"]["attributes"] == {"dn": "uni"}
    bds = _children(built)[0]["fvTenant"]["children"]
    # RN order, as merge writes it: the plan and the configuration it came from
    # can be read side by side.
    assert [next(iter(mo.values()))["attributes"]["rn"] for mo in bds] == ["BD-a", "BD-b"]


# -- what a plan refuses ----------------------------------------------------


def test_a_warning_is_refused_rather_than_written_into_a_body() -> None:
    # The report says the POST will fail; a body that quietly succeeded instead
    # would be doing something nobody read.
    with pytest.raises(ValueError) as exc:
        _plan(
            {
                "fvTenant": {
                    "attributes": {
                        "dn": "uni/tn-demo",
                        "name": "demo",
                        "descr": "prod",
                        "status": "created",
                    }
                }
            }
        )
    assert "refusing to write a plan" in str(exc.value)
    assert "1 warning" in str(exc.value)


def test_a_change_the_merged_body_cannot_hold_is_refused() -> None:
    merged = merge({"fvTenant": {"attributes": {"dn": "uni/tn-demo", "name": "demo"}}})
    with pytest.raises(ValueError) as exc:
        plan.body(merged, [Change("modified", "fvBD", "uni/tn-other/BD-bd1")])
    assert "cannot be placed" in str(exc.value)


def test_counting_leaves_the_wrapper_out() -> None:
    built = _plan(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "dev"},
                "children": [{"fvBD": {"attributes": {"name": "bd2"}}}],
            }
        }
    ).body
    assert plan.count(built) == 2
