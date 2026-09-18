"""Single entrypoint for the standalone worker launcher.

Running ``python3 <tool-dir>`` executes this file. The tool never hardcodes its own package name:
this bootstrap derives a valid import name from the directory, registers the directory as that
package, and imports the CLI by name so intra-package relative imports resolve. The directory can
therefore be renamed (even to a hyphenated name) without touching any module.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGE_NAME = "".join(
    character if (character.isalnum() or character == "_") else "_"
    for character in PACKAGE_DIR.name
)
if PACKAGE_NAME[:1].isdigit():
    PACKAGE_NAME = "_" + PACKAGE_NAME
if not PACKAGE_NAME.isidentifier():
    raise SystemExit(
        f"cannot derive an import name from directory name {PACKAGE_DIR.name!r}; "
        "rename the directory to a Python identifier"
    )

if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {PACKAGE_NAME} from {PACKAGE_DIR}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)

main = importlib.import_module(f"{PACKAGE_NAME}.cli").main

if __name__ == "__main__":
    raise SystemExit(main())
