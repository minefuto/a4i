# Every file a configuration comes from or goes to is opened here and nowhere else, so
# that a command and a model fold the identical files in the identical order.

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from a4i._validate import problems, refuse


# The files are laid end to end as one array of MOs, which a reader folds in order
# exactly as it folds the files one after another. Checking here rather than in
# a4i._merge is what lets a problem name its file: merge is handed parsed bodies.
def load(paths: list[str]) -> list[Any]:
    body: list[Any] = []
    found: list[str] = []
    for name, text in _read(paths):
        config = _parse(text, name)
        found.extend(problems(config, name))
        body.extend(config if isinstance(config, list) else [config])
    refuse(found)
    return body


def _read(paths: list[str]) -> Iterator[tuple[str, str]]:
    if not paths:
        yield "<stdin>", sys.stdin.read()
    for name in paths:
        yield name, Path(name).read_text()


# The MCP server has no shell to redirect with.
def write(path: str | Path, text: str) -> None:
    Path(path).write_text(text + "\n")


def _parse(text: str, name: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{name}: invalid JSON: {exc}") from None
