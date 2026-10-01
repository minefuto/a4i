from __future__ import annotations

import pytest

from a4i import _plan as plan
from a4i._merge import count

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


def _plan(config, imdata=CURRENT) -> dict:
    return plan.create(config, fabric=imdata)


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
    )
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
    )
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
    )
    assert _children(built) == []


def test_an_unchanged_ancestor_is_carried_as_a_container() -> None:
    built = _plan(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "dev"},
                "children": [{"fvBD": {"attributes": {"name": "bd1", "mtu": "9000"}}}],
            }
        }
    )
    tenant = _children(built)[0]["fvTenant"]
    # It is on the fabric already -- the comparison found it -- so it says so,
    # and a fabric without it refuses the POST rather than growing an empty one.
    assert tenant["attributes"] == {"rn": "tn-demo", "status": "modified"}
    assert tenant["children"][0]["fvBD"]["attributes"] == {
        "rn": "BD-bd1",
        "status": "modified",
        "mtu": "9000",
    }


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
    )
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
    )
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
    )
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


def test_counting_leaves_the_wrapper_out() -> None:
    built = _plan(
        {
            "fvTenant": {
                "attributes": {"dn": "uni/tn-demo", "name": "demo", "descr": "dev"},
                "children": [{"fvBD": {"attributes": {"name": "bd2"}}}],
            }
        }
    )
    assert count(built) == 2


def test_a_configuration_may_arrive_as_json_text() -> None:
    import json

    config = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "descr": "prod"}}}
    assert plan.create(json.dumps(config), fabric=CURRENT) == plan.create(config, fabric=CURRENT)


def test_several_configurations_are_folded_in_order() -> None:
    import json

    first = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "descr": "dev"}}}
    second = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "descr": "prod"}}}
    folded = plan.create(second, fabric=CURRENT)
    assert plan.create(first, second, fabric=CURRENT) == folded
    assert plan.create(json.dumps(first), json.dumps(second), fabric=CURRENT) == folded
    with pytest.raises(ValueError, match=r"configs\[1\]: invalid JSON"):
        plan.create(first, "{", fabric=CURRENT)


def test_an_undescribed_ancestor_is_carried_as_a_container() -> None:
    built = _plan({"fvBD": {"attributes": {"dn": "uni/tn-demo/BD-bd1", "mtu": "9000"}}})
    [tenant] = _children(built)
    assert tenant["fvTenant"]["attributes"] == {"rn": "tn-demo", "status": "modified"}


def test_an_undescribed_ancestor_the_fabric_lacks_is_refused() -> None:
    with pytest.raises(ValueError, match="1 warning"):
        _plan({"fvBD": {"attributes": {"dn": "uni/tn-other/BD-bd1"}}})
