#! python3
# -*- coding: utf-8 -*-
"""Single conversational entry point for claude-in-revit.

V0 bootstrap: sanity-check that pyRevit loads our extension, that the CPython
engine is selected (via the `#! python3` directive on line 1), and that
`lib/` is on `sys.path` so `from lib import config` resolves. The actual
LLM-in-pushbutton wiring (prompt input form, history persistence between
clicks, KG sync) is the next iteration.
"""
__title__ = "Prompt"
__doc__ = "Talk to the LLM agent (single conversational entry point)."

import os
import sys

# pyRevit puts `<extension>/lib/` on sys.path but not `<extension>/` itself,
# so `from lib import ...` doesn't resolve out of the box. Nudge sys.path
# with the extension root. See JOURNAL.md 2026-05-11 (bootstrap phase) for
# the source spelunking that uncovered this.
_EXT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _EXT_ROOT not in sys.path:
    sys.path.insert(0, _EXT_ROOT)

# `pyrevit.forms` is IronPython-only — under CPython we go straight to the
# Revit API's TaskDialog (always available inside Revit).
from Autodesk.Revit.UI import TaskDialog

from lib import config


try:
    key = config.get_api_key()
    TaskDialog.Show(
        "claude-in-revit — sanity check",
        "claude-in-revit is wired up.\n\n"
        "API key loaded: length={}, prefix='{}'.\n"
        "Engine: CPython (via the `#! python3` directive on line 1).\n"
        "lib.config import: OK (via sys.path fixup).\n\n"
        "Next phase: kg_sync.py + revit_primitives.py + the real LLM turn.".format(len(key), key[:7]),
    )
except config.ConfigError as exc:
    TaskDialog.Show(
        "claude-in-revit — config error",
        "Config error:\n\n{}".format(exc),
    )
