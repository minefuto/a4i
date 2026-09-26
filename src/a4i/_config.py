"""Where an intended configuration is read from, and where it is written back.

:mod:`a4i._merge` performs no I/O at all; this module is the other half, and every
file a configuration comes from or goes to is opened here and nowhere else, so
that a command and a model fold the identical files in the identical order.

What to do about a refused write is not decided here: each entry point words the
way out in the vocabulary of its own interface.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from a4i._validate import problems, refuse


def load(paths: list[str]) -> list[Any]:
    """Read the intended configuration from files, directories and stdin.

    A directory is walked for ``*.json`` in path order, so that a name can carry
    a numeric prefix; a path named outright is read whatever it is called, and
    ``-`` reads stdin. The reading order is the merge order, so a later file's
    attributes win.

    Every file is checked as it is read, and what is wrong with any of them is
    reported together, each named by the file it is in. Checking here rather than
    in :func:`a4i._merge.merge` is the whole reason the file names survive: merge
    is handed parsed bodies and cannot say which file one came from.

    Raises :class:`OSError` for a path that cannot be read, and
    :class:`ValueError` naming the file for one that is not JSON or that is not
    written as ACI expects.
    """

    configs: list[Any] = []
    found: list[str] = []
    for name, text in _read(paths):
        config = _parse(text, name)
        found.extend(problems(config, name))
        configs.append(config)
    refuse(found)
    return configs


def _read(paths: list[str]) -> Iterator[tuple[str, str]]:
    """Yield (name, text) for every file the paths name, in merge order."""

    for name in paths:
        if name == "-":
            yield "<stdin>", sys.stdin.read()
            continue
        path = Path(name)
        if path.is_dir():
            for file in sorted(path.rglob("*.json")):
                yield str(file), file.read_text()
        else:
            yield str(path), path.read_text()


def write(path: str | Path, text: str, *, overwrite: bool = False) -> None:
    """Write ``text`` out, refusing to replace a file that is already there.

    A shell redirect cannot do this: "> conf/all.json" truncates the file before
    a4i runs, so an output path inside the input directory would empty a source
    file and then read the empty one. This happens after everything is read.

    Raises :class:`FileExistsError` carrying the path and nothing more: the
    caller adds the way out, which is the name of its own option.
    """

    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists")
    target.write_text(text + "\n")


def _parse(text: str, name: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{name}: invalid JSON: {exc}") from None
