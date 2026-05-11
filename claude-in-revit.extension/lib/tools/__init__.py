"""Auto-import every tool module so its @tool decorators populate the registry.

Convention: any file in this package not starting with `_` is a tool module.
"""
from __future__ import annotations

from importlib import import_module
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent

for _path in sorted(_TOOLS_DIR.glob("*.py")):
    if _path.name.startswith("_"):
        continue
    import_module("{}.{}".format(__name__, _path.stem))
