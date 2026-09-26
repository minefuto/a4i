# Every file a configuration comes from or goes to is opened here and nowhere else, so
# that a command and a model fold the identical files in the identical order.

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from a4i._validate import problems, refuse


# A directory is walked for *.json in path order, so that a name can carry a numeric
# prefix. Checking here rather than in a4i._merge is what lets a problem name its file:
# merge is handed parsed bodies.
def load(paths: list[str]) -> list[Any]:
    configs: list[Any] = []
    found: list[str] = []
    for name, text in _read(paths):
        config = _parse(text, name)
        found.extend(problems(config, name))
        configs.append(config)
    refuse(found)
    return configs


def _read(paths: list[str]) -> Iterator[tuple[str, str]]:
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


# A shell redirect cannot do this: "> conf/all.json" truncates the file before a4i runs,
# so an output path inside the input directory would empty a source file and then read
# the empty one. The caller adds the way out, which is the name of its own option.
def write(path: str | Path, text: str, *, overwrite: bool = False) -> None:
    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists")
    target.write_text(text + "\n")


def _parse(text: str, name: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{name}: invalid JSON: {exc}") from None
