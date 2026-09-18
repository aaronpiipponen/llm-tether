"""The tool's operational names, kept in one place so a rename is a single edit.

The package name itself is the directory name and is resolved at runtime (see ``__main__.py``),
so it appears nowhere else in Python. The names below cannot be derived at runtime: they are the
environment-variable prefix an operator uses to point at alternate locations, and the name of the
private state directory the tool creates.
"""

from __future__ import annotations

ENV_PREFIX = "LLM_TETHER_"
STATE_DIR_NAME = "llm-tether"
