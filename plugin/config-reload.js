// config-reload — OpenCode v2 plugin
//
// Live-reloads the global config and agent/mode files without restarting OpenCode:
// provider entries added/removed by scripts, and subagent files added, edited, or
// deleted, all take effect on the next request.
//
// Why a plugin is needed: the OpenCode v2 service reads `opencode.json` once per
// location and keeps the resolved providers and models until something asks for a
// reload (`opencode reload`, `/reload` in the TUI). Nothing watches the global config
// file on its own.
//
// Mechanism: v2 calls `setup(ctx)` once per location (project directory) the service
// has open, and each ctx carries that location's `provider`, `model`, and `agent`
// services. On a change, every live ctx runs `provider.reload()` then
// `model.reload()` (and `agent.reload()` for agent/mode files), which re-reads the
// config and rebuilds the model list in place — no HTTP round trip or port discovery.
//
// Watchers are registered once per process and shared; each setup adds its ctx to
// the live set and the returned finalizer removes it when the location is disposed.
// Closing and re-registering watchers per location would race with the very reload
// they trigger. Scripts write files atomically (`foo.md.tmp` -> `foo.md`), so the
// accept filters also admit the `.tmp` temp name, otherwise the only event a
// directory watcher sees can be the temp rename and the change is missed.
//
// Placement: a top-level file in ~/.config/opencode/plugins/ is auto-loaded by both
// the service and the TUI; only the service instance does anything (see below).

import { existsSync, watch } from "node:fs"
import os from "node:os"
import path from "node:path"

const CONFIG_NAMES = new Set(["opencode.json", "opencode.jsonc", "config.json"])
const AGENT_DIRS = ["agent", "agents"]
const MODE_DIRS = ["mode", "modes"]
const STATE = Symbol.for("opencode.config-reload.v2.state")
const DEBOUNCE_MS = 500

function configDir() {
  if (process.env.XDG_CONFIG_HOME) return path.join(process.env.XDG_CONFIG_HOME, "opencode")
  return path.join(os.homedir(), ".config", "opencode")
}

function isConfigEvent(name) {
  if (CONFIG_NAMES.has(name)) return true
  if (name.endsWith(".tmp")) return CONFIG_NAMES.has(name.slice(0, -4))
  return false
}

function isMarkdownEvent(name) {
  return name.endsWith(".md") || name.endsWith(".tmp")
}

function createState() {
  const state = {
    contexts: new Set(),
    watchers: [],
    timer: null,
    pending: { config: false, agents: false },
    running: false,
    queued: false,
  }

  const reloadOne = async (ctx, work) => {
    if (work.config) {
      await ctx.provider.reload()
      await ctx.model.reload()
    }
    if (work.agents) await ctx.agent.reload()
  }

  const apply = async () => {
    if (state.running) {
      state.queued = true
      return
    }
    state.running = true
    const work = state.pending
    state.pending = { config: false, agents: false }
    try {
      for (const ctx of [...state.contexts]) {
        try {
          await reloadOne(ctx, work)
        } catch (error) {
          console.error(`config-reload: reload failed for ${ctx.location?.directory}`, error)
        }
      }
    } finally {
      state.running = false
      if (state.queued) {
        state.queued = false
        void apply()
      }
    }
  }

  const schedule = (kind) => {
    state.pending[kind] = true
    if (state.timer) clearTimeout(state.timer)
    state.timer = setTimeout(() => {
      state.timer = null
      void apply()
    }, DEBOUNCE_MS)
  }

  const watchDir = (target, accept, kind) => {
    if (!existsSync(target)) return
    try {
      const watcher = watch(target, (_event, filename) => {
        const name = filename ? String(filename) : ""
        if (name && !accept(name)) return
        schedule(kind)
      })
      watcher.unref?.()
      state.watchers.push(watcher)
    } catch (error) {
      console.error(`config-reload: cannot watch ${target}`, error)
    }
  }

  const dir = configDir()
  watchDir(dir, isConfigEvent, "config")
  for (const sub of [...AGENT_DIRS, ...MODE_DIRS]) {
    watchDir(path.join(dir, sub), isMarkdownEvent, "agents")
  }
  return state
}

// The TUI also loads top-level files from plugins/ and calls `setup` with its own
// context, which has no `provider`/`model`/`agent` services. Only the server host can
// reload, and the TUI reads providers from the server anyway, so do nothing there —
// no watchers, and no console output painting over the TUI.
function isServerHost(ctx) {
  return (
    typeof ctx?.provider?.reload === "function" &&
    typeof ctx?.model?.reload === "function" &&
    typeof ctx?.agent?.reload === "function"
  )
}

export default {
  id: "config-reload",
  setup: async (ctx) => {
    if (!isServerHost(ctx)) return
    const state = (globalThis[STATE] ??= createState())
    state.contexts.add(ctx)
    return () => {
      state.contexts.delete(ctx)
    }
  },
}
