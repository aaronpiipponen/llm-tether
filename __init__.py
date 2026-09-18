"""Start and stop llama.cpp models on the private GPU worker and forward them to this host.

This is a deliberately **standalone**, general-purpose operator tool. It depends only on the
Python standard library and never imports another project: it is meant to be copied and used
unchanged from anywhere, for any local model that runs on a private Windows/WSL2 worker. It
never reads a file relative to the current directory, so it can be run from anywhere; its only
external state is the private directory under ``$XDG_STATE_HOME`` (default
``~/.local/state/llm-tether``).

Run it with::

    python3 <tool-dir>

Running the directory executes ``__main__.py``, the single entrypoint. Bare invocation asks
what to do (start / stop / create subagent / status) and then behaves exactly as before.

Models, connection defaults, and the exposed subagent manifest all come from ``config.toml``
beside this module; add a model or subagent by editing that file, not Python. The loader
(``config.py``) fails loudly on a missing file, unknown key, wrong type, or inconsistent value.

It is intentionally NOT a long-running process. A ``start`` action launches the model detached
on the worker, forwards its loopback port to this host through a detached SSH keeper, and
returns the terminal immediately, exactly like the repository's other worker tools. A later
``stop`` action (using the same tool) terminates the selected instance and its keeper, removes
the subagent files stamped for that instance, and only when the last instance stops does it
terminate the WSL distribution to release the multi-GB model page cache the VM would otherwise
keep resident.

Several instances may run at once, including several copies of the *same* model. Each instance
has its own port, its own SSH keeper, and its own OpenCode provider entry, so all of them are
usable in OpenCode simultaneously. ``start`` adds the instance's provider entry to the OpenCode
config and ``stop`` removes it again; the tool owns that entry, and never touches any other
provider.

``create subagent`` stamps one of the bundled generic subagent templates (read-only reviewers
and a scout) with a selected live model's provider id and writes it to the global OpenCode
agents directory under a non-colliding name ``<template>-<model slug>-<instance>``. The same
template can be stamped for several live models, and one live model can hold several stamps.
Every stamped file is recorded on its instance, and stopping that instance removes all of its
stamps. OpenCode picks the changes up through the global config-reload plugin; this tool only
writes files.
"""

__all__: tuple[str, ...] = ()
