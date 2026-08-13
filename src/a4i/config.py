"""Where an intended configuration is read from, and where it is written back.

A configuration is often written in pieces -- a directory with a file per
tenant, a base and the overrides for one fabric -- and both ``a4i merge`` and the
MCP merge tool read the same pieces. The reading lives here rather than in
either of them, so a command and a model fold the identical files in the
identical order into the identical body.

:mod:`a4i.merge` performs no I/O at all, which is what makes its output a
function of its inputs and so worth keeping in git next to them. This module is
the other half: every file a configuration comes from or goes to is opened here
and nowhere else.

What to do about a refused write is not decided here. :func:`write` raises
:class:`FileExistsError` and each entry point words it in the vocabulary of its
own interface -- ``--force`` on the command line, ``overwrite: true`` over MCP --
because the way out is the caller's own and nothing this module can know.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def load(paths: list[str]) -> list[Any]:
    """Read the intended configuration from files, directories and stdin.

    A directory is walked for ``*.json`` in path order, so that a name can carry
    a numeric prefix; a path named outright is read whatever it is called, and
    ``-`` reads stdin. The reading order is the merge order, so a later file's
    attributes win.

    Raises :class:`OSError` for a path that cannot be read, and
    :class:`ValueError` naming the file for one that is not JSON.
    """

    configs: list[Any] = []
    for name in paths:
        if name == "-":
            configs.append(_parse(sys.stdin.read(), "<stdin>"))
            continue
        path = Path(name)
        if path.is_dir():
            configs.extend(_parse(f.read_text(), str(f)) for f in sorted(path.rglob("*.json")))
        else:
            configs.append(_parse(path.read_text(), str(path)))
    return configs


def write(path: str | Path, text: str, *, overwrite: bool = False) -> None:
    """Write ``text`` out, refusing to replace a file that is already there.

    A shell redirect cannot do this: "> conf/all.json" truncates the file before
    a4i runs, so an output path inside the input directory would empty a source
    file and then read the empty file. Writing it here happens after everything
    has been read, and an existing file is refused outright unless ``overwrite``
    says otherwise -- the paths a merge is pointed at are exactly the paths its
    output is easiest to confuse with.

    Raises :class:`FileExistsError` for that refusal. It carries the path and
    nothing more: the caller adds the way out, which is the name of its own
    option and not something this module has any way to know.
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
