# FORCE_COLOR makes rich colour pytest's captured streams, whose highlighter then breaks
# a message into coloured runs and fails every assertion on a substring of one. It is
# dropped here, before the first Console is built.

from __future__ import annotations

import os

os.environ.pop("FORCE_COLOR", None)
os.environ["NO_COLOR"] = "1"
