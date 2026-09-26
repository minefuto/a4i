from __future__ import annotations

import a4i
from a4i import _diff, _dry_run, _merge, _plan


def test_the_package_exports_the_commands_under_their_cli_names() -> None:
    assert a4i.merge is _merge.merge
    assert a4i.diff is _diff.compare
    assert a4i.plan is _plan.create
    assert a4i.dry_run is _dry_run.check
    assert dir(a4i) == sorted(a4i.__all__)
