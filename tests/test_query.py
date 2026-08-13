from __future__ import annotations

import pytest

from a4i import query


def test_build_path_class_vs_mo() -> None:
    # Nothing about the string decides this any more: the same text is a class
    # path or an MO path depending only on the kind it is read as.
    assert query.build_path("fvTenant", "class") == "/api/class/fvTenant.json"
    assert query.build_path("uni/tn-common", "mo") == "/api/mo/uni/tn-common.json"
    assert query.build_path("uni/tn-common", "class") == "/api/class/uni/tn-common.json"


def test_build_path_toplevel_mo() -> None:
    assert query.build_path("uni", "mo") == "/api/mo/uni.json"


def test_build_path_normalizes_whitespace_and_slashes() -> None:
    # A leading "/" used to be what marked a DN; it is now just noise to drop, so
    # a DN typed the old way still works.
    assert query.build_path("/uni/tn-common", "mo") == "/api/mo/uni/tn-common.json"
    assert query.build_path("  /uni/tn-common/  ", "mo") == "/api/mo/uni/tn-common.json"
    assert query.build_path(" fvTenant ", "class") == "/api/class/fvTenant.json"


def test_build_path_rejects_an_unknown_kind() -> None:
    assert query.KINDS == ("class", "mo")
    with pytest.raises(ValueError, match="invalid kind: 'dn'"):
        query.build_path("uni", "dn")
