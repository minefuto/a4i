from __future__ import annotations

import io
import json

import pytest

from a4i import config

BASE = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "descr": "wrong"}}}
OVERRIDE = {"fvTenant": {"attributes": {"dn": "uni/tn-demo", "descr": "right"}}}


def _write(path, name: str, body) -> str:
    file = path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(body if isinstance(body, str) else json.dumps(body))
    return str(file)


# -- reading ---------------------------------------------------------------


def test_a_named_file_is_read_whatever_it_is_called(tmp_path) -> None:
    assert config.load([_write(tmp_path, "intended.txt", BASE)]) == [BASE]


def test_a_directory_is_read_in_path_order(tmp_path) -> None:
    # The reading order is the merge order, so a numeric prefix is what decides
    # which file's value survives.
    _write(tmp_path, "20-rest.json", OVERRIDE)
    _write(tmp_path, "10-base.json", BASE)
    assert config.load([str(tmp_path)]) == [BASE, OVERRIDE]


def test_a_directory_is_walked_recursively_and_nothing_but_json_is_read(tmp_path) -> None:
    _write(tmp_path, "sub/tn.json", BASE)
    _write(tmp_path, "README.md", "not json at all")
    _write(tmp_path, "tn.json.bak", "{broken")
    assert config.load([str(tmp_path)]) == [BASE]


def test_the_paths_are_read_in_the_order_given(tmp_path) -> None:
    wrong = _write(tmp_path, "a.json", BASE)
    right = _write(tmp_path, "b.json", OVERRIDE)
    assert config.load([wrong, right]) == [BASE, OVERRIDE]
    # The other way round, the other value is the one that would survive.
    assert config.load([right, wrong]) == [OVERRIDE, BASE]


def test_a_dash_reads_stdin(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(BASE)))
    assert config.load(["-"]) == [BASE]


def test_a_file_that_is_not_json_is_named_in_the_error(tmp_path) -> None:
    with pytest.raises(ValueError) as exc:
        config.load([_write(tmp_path, "broken.json", "{not json")])
    assert "broken.json" in str(exc.value)
    assert "invalid JSON" in str(exc.value)


def test_a_path_that_is_not_there_is_an_oserror(tmp_path) -> None:
    with pytest.raises(OSError):
        config.load([str(tmp_path / "gone.json")])


def test_finding_no_configuration_is_not_an_error(tmp_path) -> None:
    # A directory holding nothing to read yields nothing. What that means is
    # merge's to decide, not this module's.
    _write(tmp_path, "README.md", "not json at all")
    assert config.load([str(tmp_path)]) == []


# -- writing ---------------------------------------------------------------


def test_the_text_is_written_with_a_trailing_newline(tmp_path) -> None:
    out = tmp_path / "merged.json"
    config.write(out, '{"a": 1}')
    assert out.read_text() == '{"a": 1}\n'


def test_a_file_that_is_already_there_is_refused(tmp_path) -> None:
    # "> conf/all.json" would truncate the file before a4i ran, and an output
    # path inside the input directory is the easy mistake to make.
    out = tmp_path / "tn.json"
    out.write_text("keep me")
    with pytest.raises(FileExistsError) as exc:
        config.write(out, "replaced")
    assert str(out) in str(exc.value)
    assert out.read_text() == "keep me"


def test_the_refusal_names_no_way_out(tmp_path) -> None:
    """The way out belongs to the caller's interface, so this must not guess it.

    'a4i merge' says --force and the MCP merge tool says overwrite: true. A
    remedy written here would be wrong for one of them, and each entry point
    adds its own.
    """

    out = tmp_path / "tn.json"
    out.write_text("keep me")
    with pytest.raises(FileExistsError) as exc:
        config.write(out, "replaced")
    assert "--force" not in str(exc.value)
    assert "overwrite" not in str(exc.value)


def test_an_existing_file_is_replaced_when_told_to(tmp_path) -> None:
    out = tmp_path / "tn.json"
    out.write_text("keep me")
    config.write(out, "replaced", overwrite=True)
    assert out.read_text() == "replaced\n"


def test_the_output_path_may_be_a_string_or_a_path(tmp_path) -> None:
    config.write(str(tmp_path / "a.json"), "1")
    config.write(tmp_path / "b.json", "2")
    assert (tmp_path / "a.json").read_text() == "1\n"
    assert (tmp_path / "b.json").read_text() == "2\n"
