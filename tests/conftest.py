"""Settings the whole test suite depends on.

``rich`` colours a stream it is told to colour, and ``FORCE_COLOR`` tells it to
whether or not the stream is a terminal -- which pytest's captured ones are not.
Its highlighter then breaks an error message into coloured runs, so a test
asserting on a substring of one fails on any machine that sets the variable.
Dropping it here, before the first ``Console`` is built, is what keeps the
assertions about what a command prints independent of where they are run.
"""

from __future__ import annotations

import os

os.environ.pop("FORCE_COLOR", None)
os.environ["NO_COLOR"] = "1"
