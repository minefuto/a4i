from __future__ import annotations

import pytest

from a4i.validate import check, problems


def mo(class_name: str, attributes: dict, children: list | None = None) -> dict:
    body: dict = {"attributes": attributes}
    if children is not None:
        body["children"] = children
    return {class_name: body}


def only(config, source: str | None = None) -> str:
    """Return the one problem the config has, failing if it has any other number."""

    (found,) = problems(config, source)
    return found


# -- what is not an MO -----------------------------------------------------


@pytest.mark.parametrize("element", ["fvTenant", 5, None, True, [[]]])
def test_an_element_that_is_not_an_object_is_refused(element) -> None:
    assert "not an MO" in only([element])


def test_an_empty_object_in_an_array_is_refused() -> None:
    assert "describes no MO" in only([mo("fvTenant", {"name": "t"}), {}])


def test_two_mos_written_as_one_object_are_refused() -> None:
    found = only([{"fvTenant": {"attributes": {}}, "fvBD": {"attributes": {}}}])
    assert "2 keys" in found
    assert '"fvTenant", "fvBD"' in found


@pytest.mark.parametrize("body", [None, "t", [{"attributes": {}}]])
def test_a_class_mapping_to_something_other_than_a_body_is_refused(body) -> None:
    assert '"attributes" level' in only([{"fvTenant": body}])


def test_a_configuration_that_is_neither_an_mo_nor_an_array_is_refused() -> None:
    assert "array of MOs" in only("fvTenant")


# -- a GET response fed back as a configuration ----------------------------


@pytest.mark.parametrize(
    "response",
    [{"totalCount": "1", "imdata": [{"fvTenant": {"attributes": {}}}]}, {"imdata": []}],
)
def test_a_get_response_is_named_for_what_it_is(response) -> None:
    # The commonest way to write a configuration that quietly describes nothing,
    # and the generic "2 keys" complaint would not lead anyone to the fix.
    assert "GET response" in only(response)
    assert "imdata" in only(response)


# -- the body --------------------------------------------------------------


def test_an_unknown_key_in_a_body_is_refused() -> None:
    # "attribute" for "attributes": the MO would merge carrying nothing at all.
    assert '"attribute"' in only({"fvTenant": {"attribute": {"name": "t"}}})


def test_attributes_that_are_not_an_object_are_refused() -> None:
    # This used to reach a4i.mo.child_dn and raise AttributeError there.
    assert '"attributes" is an array' in only({"fvTenant": {"attributes": [{"name": "t"}]}})


def test_children_that_are_not_an_array_are_refused() -> None:
    # Iterating an object yields its keys, so every child was dropped in silence.
    body = {"fvTenant": {"attributes": {"name": "t"}, "children": {"fvBD": {"attributes": {}}}}}
    assert '"children" is an object' in only(body)


def test_an_mo_giving_neither_attributes_nor_children_is_refused() -> None:
    assert "nothing to configure" in only({"fvTenant": {}})


def test_a_class_the_dictionary_lacks_is_refused_when_it_says_nothing_either() -> None:
    # A pseudo RN would key it as "zzUnknown[]" and it would reach the APIC,
    # which has no way to build an RN from nothing.
    assert "nothing to configure" in only({"zzUnknown": {"attributes": {}}})


def test_an_empty_poluni_is_no_mistake() -> None:
    # A file that describes nothing yet: the wrapper is not configuration, so
    # there is nothing for it to say.
    assert problems({"polUni": {}}) == []
    assert problems({"polUni": {"attributes": {"dn": "uni"}, "children": []}}) == []


# -- attribute values ------------------------------------------------------


def test_a_string_and_a_number_are_both_accepted() -> None:
    assert problems(mo("fvBD", {"name": "bd1", "mtu": 9000})) == []


def test_an_empty_string_is_a_value_and_not_a_mistake() -> None:
    # It is the only way to clear a property.
    assert problems(mo("fvTenant", {"name": "t", "descr": ""})) == []


def test_a_null_attribute_value_is_refused() -> None:
    # It used to be merged as the string "None" and posted.
    assert "is null" in only(mo("fvTenant", {"name": "t", "descr": None}))


def test_a_boolean_attribute_value_is_refused() -> None:
    # It used to be merged as "True", which no ACI property takes, so a diff
    # went on reporting the attribute for ever.
    found = only(mo("fvBD", {"name": "bd1", "unicastRoute": True}))
    assert "is a boolean" in found
    assert '"yes"' in found


@pytest.mark.parametrize("value", [{"a": "b"}, ["a"]])
def test_a_structured_attribute_value_is_refused(value) -> None:
    assert "attribute value is a string" in only(mo("fvTenant", {"name": "t", "descr": value}))


def test_every_bad_value_of_one_mo_is_reported() -> None:
    assert len(problems(mo("fvTenant", {"name": None, "descr": True}))) == 2


# -- saying where ----------------------------------------------------------


def test_a_problem_names_the_file_the_position_and_the_parent() -> None:
    config = [mo("fvTenant", {"name": "t"}, [mo("fvBD", {"name": "bd1"}), "fvBD"])]
    found = only(config, "conf/10-base.json")
    assert found.startswith("conf/10-base.json: [0].children[1] (child of uni/tn-t):")


def test_a_root_element_is_named_by_its_position_alone() -> None:
    # "(child of uni)" would be saying that every root MO hangs under uni.
    assert only(["fvTenant"], "conf/a.json").startswith("conf/a.json: [0]:")


def test_a_body_given_on_its_own_is_named_by_neither() -> None:
    assert only({"fvTenant": {}}).startswith("the body:")


def test_the_children_of_a_wrapper_hang_under_uni() -> None:
    assert only({"polUni": {"children": ["fvTenant"]}}).startswith("children[0]:")


def test_a_child_of_an_mo_no_dn_can_be_worked_out_for_is_named_by_position() -> None:
    # The parent gives nothing its RN is built from, so a DN for it would be a
    # place that does not exist. merge refuses the parent for that separately.
    found = only([mo("fvTenant", {"descr": "x"}, ["fvBD"])])
    assert found.startswith("[0].children[0]:")


def test_a_problem_deeper_down_carries_the_dn_of_its_parent() -> None:
    config = mo("fvTenant", {"name": "t"}, [mo("fvBD", {"name": "b"}, [{"fvRsCtx": None}])])
    assert "(child of uni/tn-t/BD-b)" in only(config)


# -- how they are reported -------------------------------------------------


def test_a_well_formed_configuration_has_nothing_to_report() -> None:
    config = [mo("fvTenant", {"name": "t"}, [mo("fvBD", {"name": "b", "mtu": "9000"})])]
    assert problems(config) == []
    check(config)


@pytest.mark.parametrize("nothing", [[], {}])
def test_an_input_describing_nothing_is_well_formed(nothing) -> None:
    # A placeholder file among real ones. Whether the inputs describe any MO
    # between them is merge's question, not this one.
    assert problems(nothing) == []


def test_the_first_few_problems_are_spelled_out_and_the_rest_counted() -> None:
    with pytest.raises(ValueError) as exc:
        check(["a", "b", "c", "d", "e"])
    message = str(exc.value)
    assert "in 5 places" in message
    assert message.count("not an MO") == 3
    assert "and 2 more" in message


def test_one_problem_is_reported_without_a_count() -> None:
    with pytest.raises(ValueError) as exc:
        check(["a"])
    assert "places" not in str(exc.value)


def test_problems_are_reported_in_reading_order() -> None:
    found = problems([mo("fvTenant", {"name": "t"}, ["a", "b"]), "c"])
    assert [line.split(":")[0] for line in found] == [
        "[0].children[0] (child of uni/tn-t)",
        "[0].children[1] (child of uni/tn-t)",
        "[1]",
    ]
