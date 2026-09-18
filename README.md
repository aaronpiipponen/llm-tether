# llm-tether

A small, standalone operator tool for running [llama.cpp](https://github.com/ggml-org/llama.cpp)
models on a private Windows/WSL2 GPU worker and exposing them to a local
[OpenCode](https://opencode.ai) instance.

`llm-tether` starts a `llama-server` on the worker over SSH, forwards its loopback port to this
machine through a detached SSH keeper, and adds one OpenCode provider entry per running instance.
It also stamps read-only subagent templates into the global OpenCode agents directory. Everything
it needs is described by one `config.toml`; there is no other external state.

It is deliberately stdlib-only and self-contained: it does not import, and does not depend on, any
other project. Run it from anywhere; it never reads a file relative to the current directory.

## Requirements

- Python 3.14 or newer (the code uses `tomllib` and modern `except` syntax).
- An SSH-reachable Windows host with a WSL2 distribution.
- Pinned `llama.cpp` builds on the worker, one directory per revision:
  `~/.local/src/llama.cpp-<runtime_revision>/build/bin/llama-server`.
- GGUF files on the worker, referenced by path relative to the worker checkout root.
- An AMD GPU; the launcher exports `HSA_ENABLE_DXG_DETECTION=1` before starting `llama-server`.
- OpenCode, for the provider entries and subagents to be useful.

## Install

Copy the tool directory somewhere on your machine, for example `~/projects/tools/llm-tether/`. There is
nothing to build and no package to install; run it directly:

```sh
python3 ~/projects/tools/llm-tether
```

Running a directory executes its `__main__.py`. A bare invocation opens the interactive menu. An
action can also be passed as a flag for scripting; both paths call the same functions.

## Configure

```sh
cp config.toml.example config.toml
$EDITOR config.toml
```

`config.toml` is the single source for the connection, the model catalog, and the exposed subagent
manifest. The example documents every field inline. The loader is strict: a missing file, unknown
key, wrong type, or inconsistent value stops the command with a clear message. You must add at
least one active `[[models]]` table before any command will run.

All paths are configured, never hardcoded. To keep a config outside the tool directory, point
`LLM_TETHER_CONFIG` at it:

```sh
LLM_TETHER_CONFIG=~/.config/llm-tether/config.toml python3 ~/projects/tools/llm-tether status
```

Every `[connection]` value can also be overridden per invocation with `LLM_TETHER_SSH_TARGET`,
`LLM_TETHER_WSL_DISTRO`, and `LLM_TETHER_WORKER_ROOT`. The remaining overrides, mostly useful
for testing, are `LLM_TETHER_STATE_DIR`, `LLM_TETHER_OPENCODE_CONFIG`, and
`LLM_TETHER_OPENCODE_AGENTS_DIR`.

## Usage

Run the menu:

```sh
python3 ~/projects/tools/llm-tether
```

Or pass an action. Common commands:

```sh
# Start a model; context and reasoning default to interactive selection.
python3 ~/projects/tools/llm-tether start --model <model-key> --context 32768 --reasoning off

# Start three copies of it; runs are serial and can take minutes.
python3 ~/projects/tools/llm-tether start --model <model-key> --count 3

# List the configured model catalog, with context, reasoning, and VRAM estimates.
python3 ~/projects/tools/llm-tether models
python3 ~/projects/tools/llm-tether models --json

# Show which config, state, OpenCode, agents, and plugin paths are in effect.
python3 ~/projects/tools/llm-tether where

# Preview a start without touching the worker.
python3 ~/projects/tools/llm-tether start --model <model-key> --dry-run

# Show running instances, and optionally each one's OpenCode subagents and VRAM.
python3 ~/projects/tools/llm-tether status
python3 ~/projects/tools/llm-tether status --json
python3 ~/projects/tools/llm-tether status --vram

# Inspect the remote llama-server log and the local keeper log for one instance.
python3 ~/projects/tools/llm-tether logs --model <model-key> --instance 1 --lines 200

# Stamp a subagent (or every configured template) for a running provider.
python3 ~/projects/tools/llm-tether create-subagent --provider local-<model-key> --template <name>
python3 ~/projects/tools/llm-tether create-subagent --provider local-<model-key> --all --dry-run
python3 ~/projects/tools/llm-tether subagents
python3 ~/projects/tools/llm-tether remove-subagent --provider local-<model-key> --template <name>
python3 ~/projects/tools/llm-tether remove-subagent --provider local-<model-key> --file <name>.md

# Recover from drift; these default to a dry run.
python3 ~/projects/tools/llm-tether doctor
python3 ~/projects/tools/llm-tether reconcile
python3 ~/projects/tools/llm-tether reconcile --execute
python3 ~/projects/tools/llm-tether prune-subagents --execute
python3 ~/projects/tools/llm-tether clean
python3 ~/projects/tools/llm-tether clean --execute

# Restart an instance in place; it gets a new index and provider id.
python3 ~/projects/tools/llm-tether restart --provider local-<model-key>

# Stop one by provider, by model, or all instances.
python3 ~/projects/tools/llm-tether stop --provider local-<model-key>
python3 ~/projects/tools/llm-tether stop --model <model-key> --instance 1
python3 ~/projects/tools/llm-tether stop --all
```

Run `python3 ~/projects/tools/llm-tether --help` for the full flag list; the module docstrings in
`cli.py`, `lifecycle.py`, and `reconcile.py` describe the actions in more detail.

Key behaviors:

- A `start` launches the model detached, forwards its port through a detached SSH keeper, and
  returns immediately. The tool is not a long-running process.
- Several instances, including several copies of the same model, can run at once. Each has its
  own port, keeper, provider entry, and stamped subagents.
- `stop` removes that instance's provider entry and stamped subagents. Only when the last instance
  stops does it terminate the WSL distribution, releasing the multi-GB model page cache the VM
  would otherwise keep resident.
- `reconcile` re-adds providers for running instances that lost their entry, drops dead state
  entries, prunes orphan stamps, and re-stamps recorded agent files that went missing.
- `clean` removes local keeper logs and remote logs/pidfiles for instances that have no state
  entry.
- `reconcile`, `prune-subagents`, and `clean` only report by default. Pass `--execute` to apply.
- `doctor` prints the resolved paths and warns about stale keeper logs. It never writes.

## Live reload

Editing `opencode.json` or the agents directory only takes effect the next time OpenCode reads
them. To make changes appear in a running OpenCode, install the bundled plugin:

```sh
cp plugin/config-reload.js ~/.config/opencode/plugins/config-reload.js
```

It watches the global config and the `agents`/`modes` directories and asks the running server to
reload. Without it, restart OpenCode after `start`, `stop`, or `create-subagent`. `doctor` checks
for the plugin and warns when it is absent.

## State and logs

- Private state: `$XDG_STATE_HOME/llm-tether/state.json` (default
  `~/.local/state/llm-tether/state.json`). It records the running instances and the subagent
  files each one owns. This is the source of truth for `stop`, `status`, and `reconcile`.
- Local keeper logs: the same directory, `<model-key>-<instance>.keeper.log`.
- Remote logs: `~/.local/state/llm-tether/<model-key>-<instance>.log` on the worker.

## Troubleshooting

- `python3 ~/projects/tools/llm-tether doctor` cross-checks the config, templates, OpenCode config, agents
  directory, recorded state, health endpoints, and the reload plugin. Add `--json` for scripting.
- A failed `start` leaves no session. Use `logs --model <key> --instance <n>` to read the remote
  `llama-server` log; it addresses the file directly, so no state entry is needed.
- `reconcile` re-adds providers for running instances that lost their config entry, removes
  `local-*` providers with no owner, drops dead state entries, prunes orphan stamps, and
  re-stamps recorded agent files that are missing. `clean` removes stale logs for gone instances.

## Tests

The test suite is stdlib `unittest` and builds every case in a throwaway workspace, so it never
touches a real config, state file, OpenCode config, or agents directory:

```sh
python3 tests/run.py
```

## Limitations and portability

- `logs` is a one-shot `tail` over SSH. It cannot follow a running log.
- `status --vram` shells out to the worker (one probe per runtime revision); it is opt-in.
- `restart` produces a new provider id because the instance index advances. Update anything that
  referenced the old id.
- The tool owns only OpenCode providers whose id starts with `local-`; it never touches others.
- The tool requires Python 3.14+; the code is formatted with the repository's `ruff` settings.
