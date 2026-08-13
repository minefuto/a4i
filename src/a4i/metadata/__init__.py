"""Bundled static dictionaries of the ACI management information model.

All of them come from the Cisco APIC MIM Reference, which serves one JSON file
per class under
``https://pubhub.devnetcloud.com/media/model-doc-latest/docs/doc/jsonmeta/<pkg>/<Class>.json``
-- the package being the leading lowercase run of the class name, so
``fvnsEncapBlk`` is ``fvns/EncapBlk.json``. ``tools/gen_metadata.py`` regenerates
every file here from that source; nothing in this directory is written by hand.

``classes.txt`` lists the concrete (non-abstract, non-deprecated) class names,
one per line, and is what ``a4i list class`` prints. It is stored sorted and
de-duplicated so that loading it is a split rather than a parse and a sort.
Shell completion does not read it, or anything else here: a tab press is answered
from the argparse parser alone, so it costs one process start and no I/O. See
:mod:`a4i.completion`.

``rn_formats.txt`` maps a class to its ``rnFormat`` -- ``fvBD``, tab, ``BD-{name}``
-- which is how a body's child MO is turned into a DN (:mod:`a4i.mo`). Only
classes the JSON marks ``isConfigurable`` are kept: an MO on the fabric arrives
with its DN already, so a format is only ever needed for one an input asks for.

``model.jsonl`` carries one distilled record per class -- what it is for, what
may hang under it, and every property with its type, its permitted values and
its default -- and ``model.idx`` says where each record starts. A record is
reached by one seek rather than by reading the file, because the file is some
megabytes and a caller wants one class of it. This is what ``a4i describe`` and
the MCP server's ``describe`` tool both serve.

``search.txt`` is the class name, label and one-line summary of each class, for
finding a class by what it is called rather than by how its name begins --
``a4i search`` and the MCP tool of that name.

Nothing here runs in the completion path, so every file loads lazily and costs
nothing until something asks.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CLASSES_FILE = Path(__file__).with_name("classes.txt")
_RN_FORMATS_FILE = Path(__file__).with_name("rn_formats.txt")
_MODEL_FILE = Path(__file__).with_name("model.jsonl")
_MODEL_INDEX_FILE = Path(__file__).with_name("model.idx")
_SEARCH_FILE = Path(__file__).with_name("search.txt")


@lru_cache(maxsize=1)
def load_class_names() -> tuple[str, ...]:
    """Return the bundled ACI class names, sorted and de-duplicated in the file."""

    return tuple(_CLASSES_FILE.read_text(encoding="utf-8").split())


def class_names_startingwith(prefix: str) -> list[str]:
    """Return bundled class names matching ``prefix`` (case-insensitive)."""

    lowered = prefix.lower()
    return [name for name in load_class_names() if name.lower().startswith(lowered)]


@lru_cache(maxsize=1)
def load_rn_formats() -> dict[str, str]:
    """Return the bundled RN format of every configurable class, keyed by class."""

    text = _RN_FORMATS_FILE.read_text(encoding="utf-8")
    return dict(line.split("\t", 1) for line in text.splitlines() if line)


def rn_format(class_name: str) -> str | None:
    """Return how ``class_name`` builds its RN, or None when it is not bundled."""

    return load_rn_formats().get(class_name)


# -- the model ------------------------------------------------------------


@lru_cache(maxsize=1)
def _model_index() -> dict[str, tuple[int, int]]:
    """Return where each class's record sits in ``model.jsonl``: (offset, length)."""

    index: dict[str, tuple[int, int]] = {}
    with _MODEL_INDEX_FILE.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            name, offset, length = line.rstrip("\n").split("\t")
            index[name] = (int(offset), int(length))
    return index


def describe(class_name: str) -> dict[str, Any] | None:
    """Return the bundled record for ``class_name``, or None when there is none.

    Matching is exact and case-sensitive, as ACI class names are. A class the
    dictionary does not carry is not an error: the fabric may be running a
    release newer than the dictionary, and a caller can still query it.
    """

    location = _model_index().get(class_name)
    if location is None:
        return None
    offset, length = location
    with _MODEL_FILE.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(length)
    return json.loads(raw)


@lru_cache(maxsize=1)
def _search_entries() -> tuple[tuple[str, int, str, str], ...]:
    """Return (class, rank, label, summary) for every class, in class-name order.

    ``rank`` says what kind of class it is, lowest first: an ordinary
    configurable one, a relation between two, one that can only be read, and an
    abstract one. See ``rank`` in ``tools/gen_metadata.py``.
    """

    entries: list[tuple[str, int, str, str]] = []
    with _SEARCH_FILE.open(encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 4:
                entries.append((parts[0], int(parts[1]), parts[2], parts[3]))
    return tuple(entries)


def search(keyword: str, limit: int = 40) -> list[tuple[str, str, str]]:
    """Return the classes whose name, label or summary contains ``keyword``.

    Matching is a case-insensitive substring, which is deliberately not what
    ``a4i list class`` does: that one matches a prefix of the name and nothing
    else, so that its answer is exact and instant. This one is the other
    question -- which class is the one for bridge domains -- and a substring over
    the labels is what answers it.

    Three things order the results. Where the match landed comes first: an exact
    label, then the name, then the label, then the summary. Within that, what
    kind of class it is: dozens of classes are labelled "Bridge Domain", and only
    one of them is the bridge domain -- the rest are the relations pointing at it
    and the abstract policy behind it. Last, the shorter name, which is what
    tells ``fvAEPg`` from ``cloudAEPgSelector``: ACI names a thing after itself
    and everything else after what it qualifies. All three matter, because a
    caller with a limit of 3 sees only the top of this list.
    """

    lowered = keyword.strip().lower()
    if not lowered:
        return []
    buckets: tuple[list[tuple[str, int, str, str]], ...] = ([], [], [], [])
    for entry in _search_entries():
        name, _, label, summary = entry
        if label.lower() == lowered:
            buckets[0].append(entry)
        elif lowered in name.lower():
            buckets[1].append(entry)
        elif lowered in label.lower():
            buckets[2].append(entry)
        elif lowered in summary.lower():
            buckets[3].append(entry)
    ordered = [
        entry
        for bucket in buckets
        for entry in sorted(bucket, key=lambda e: (e[1], len(e[0]), e[0]))
    ]
    return [(name, label, summary) for name, _, label, summary in ordered[:limit]]
