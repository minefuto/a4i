from __future__ import annotations

import io
import json

import pytest

from a4i import _config as config

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


def test_the_paths_are_read_in_the_order_given(tmp_path) -> None:
    wrong = _write(tmp_path, "a.json", BASE)
    right = _write(tmp_path, "b.json", OVERRIDE)
    assert config.load([wrong, right]) == [BASE, OVERRIDE]
    # The other way round, the other value is the one that would survive.
    assert config.load([right, wrong]) == [OVERRIDE, BASE]


def test_no_path_reads_stdin(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(BASE)))
    assert config.load([]) == [BASE]


def test_a_file_that_is_not_json_is_named_in_the_error(tmp_path) -> None:
    with pytest.raises(ValueError) as exc:
        config.load([_write(tmp_path, "broken.json", "{not json")])
    assert "broken.json" in str(exc.value)
    assert "invalid JSON" in str(exc.value)


def test_a_path_that_is_not_there_is_an_oserror(tmp_path) -> None:
    with pytest.raises(OSError):
        config.load([str(tmp_path / "gone.json")])


def test_a_file_that_is_not_written_as_aci_expects_is_named_in_the_error(tmp_path) -> None:
    # The whole reason the check runs here: merge is handed parsed bodies and
    # could not say which of thirty files the bad element sits in.
    good = _write(tmp_path, "10-good.json", BASE)
    bad = _write(tmp_path, "20-bad.json", [BASE, {"totalCount": "1", "imdata": []}])
    with pytest.raises(ValueError) as exc:
        config.load([good, bad])
    assert "20-bad.json: [1]" in str(exc.value)
    assert "10-good.json" not in str(exc.value)


def test_every_file_is_read_before_any_of_them_is_refused(tmp_path) -> None:
    # One run names everything to fix, rather than one file per run.
    a = _write(tmp_path, "10-a.json", ["a"])
    b = _write(tmp_path, "20-b.json", ["b"])
    with pytest.raises(ValueError) as exc:
        config.load([a, b])
    assert "10-a.json" in str(exc.value)
    assert "20-b.json" in str(exc.value)


def test_a_file_describing_nothing_is_no_bar_to_the_ones_that_do(tmp_path) -> None:
    placeholder = _write(tmp_path, "10-placeholder.json", [])
    tn = _write(tmp_path, "20-tn.json", BASE)
    assert config.load([placeholder, tn]) == [BASE]


# -- writing ---------------------------------------------------------------


def test_the_text_is_written_with_a_trailing_newline(tmp_path) -> None:
    out = tmp_path / "merged.json"
    config.write(out, '{"a": 1}')
    assert out.read_text() == '{"a": 1}\n'


def test_a_file_that_is_already_there_is_refused(tmp_path) -> None:
    out = tmp_path / "tn.json"
    out.write_text("keep me")
    with pytest.raises(FileExistsError) as exc:
        config.write(out, "replaced")
    assert str(out) in str(exc.value)
    assert out.read_text() == "keep me"


# The MCP merge and plan tools each phrase the way out in their own words.
def test_the_refusal_names_no_way_out(tmp_path) -> None:
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
