// config-reload — opencode plugin
//
// Live-reloads global config and agent/mode files without restarting opencode:
// provider entries added/removed by scripts, and subagent files added, edited,
// or deleted, all take effect on the next request.
//
// Why a plugin is needed: the global config is cached for the process lifetime
// (`Config.getGlobal` uses `Effect.cachedInvalidateWithTTL` with
// `Duration.infinity`), and agent/mode files are only rescanned when an instance
// is (re)created. Nothing watches `~/.config/opencode` on its own.
//
// Mechanism: on change, call the running server's own HTTP API —
//   1. `PATCH /global/config` (`Config.updateGlobal`) clears the cached global
//      config and disposes all instances, so provider entries are re-read.
//   2. `POST /global/dispose` forces instance teardown so agent/mode directories
//      are rescanned the next time an instance is built (covers add/edit/delete).
// The SDK client handed to the plugin is already bound to this server (base URL
// and auth headers), so no port discovery or signalling is required. (The TUI's
// SIGUSR2 handler runs on a runtime that does not clear the HTTP server's
// provider cache and often fails to recreate instances, so it is not used.)
//
// Watchers are registered once per process and kept, updating the bound client on
// re-instantiation; closing and re-registering them per instance (the previous
// approach) raced with the very reload they triggered and silently dropped
// events. Scripts write files atomically (`foo.md.tmp` -> `foo.md`), so the
// accept filters also admit the `.tmp` temp name, otherwise the only event a
// directory watcher sees can be the temp rename and the change is missed.
//
// Placement: a top-level file in ~/.config/opencode/plugins/ is auto-loaded.

import { existsSync, readFileSync, watch } from "node:fs"
import os from "node:os"
import path from "node:path"

const CONFIG_NAMES = new Set(["opencode.json", "opencode.jsonc", "config.json"])
const AGENT_DIRS = ["agent", "agents"]
const MODE_DIRS = ["mode", "modes"]
const STATE = Symbol.for("opencode.config-reload.state")
const DEBOUNCE_MS = 500

function configDir() {
  if (process.env.XDG_CONFIG_HOME) return path.join(process.env.XDG_CONFIG_HOME, "opencode")
  return path.join(os.homedir(), ".config", "opencode")
}

function configFile(dir) {
  for (const name of ["opencode.jsonc", "opencode.json", "config.json"]) {
    const file = path.join(dir, name)
    if (existsSync(file)) return file
  }
  return undefined
}

function isConfigEvent(name) {
  if (CONFIG_NAMES.has(name)) return true
  if (name.endsWith(".tmp")) return CONFIG_NAMES.has(name.slice(0, -4))
  return false
}

function isMarkdownEvent(name) {
  return name.endsWith(".md") || name.endsWith(".tmp")
}

export const ConfigReload = async ({ client }) => {
  const existing = globalThis[STATE]
  if (existing) {
    // Re-instantiation: keep the watchers, rebind to the newest client.
    existing.client = client
    return {}
  }

  const state = {
    client,
    dir: configDir(),
    watchers: [],
    timer: null,
    running: false,
    queued: false,
  }
  globalThis[STATE] = state

  const apply = async () => {
    if (state.running) {
      state.queued = true
      return
    }
    state.running = true
    try {
      const file = configFile(state.dir)
      if (file) {
        let body
        try {
          body = JSON.parse(readFileSync(file, "utf8"))
        } catch {
          body = undefined
        }
        if (body !== undefined) {
          await state.client._client.patch({
            url: "/global/config",
            body,
            headers: { "Content-Type": "application/json" },
            throwOnError: false,
          })
        }
      }
      await state.client._client.post({ url: "/global/dispose", throwOnError: false })
    } catch (error) {
      console.error("config-reload: reload failed", error)
    } finally {
      state.running = false
      if (state.queued) {
        state.queued = false
        void apply()
      }
    }
  }

  const schedule = () => {
    if (state.timer) clearTimeout(state.timer)
    state.timer = setTimeout(() => {
      state.timer = null
      void apply()
    }, DEBOUNCE_MS)
  }

  const watchDir = (target, accept) => {
    if (!existsSync(target)) return
    try {
      const watcher = watch(target, (_event, filename) => {
        const name = filename ? String(filename) : ""
        if (name && !accept(name)) return
        schedule()
      })
      watcher.unref?.()
      state.watchers.push(watcher)
    } catch (error) {
      console.error(`config-reload: cannot watch ${target}`, error)
    }
  }

  watchDir(state.dir, isConfigEvent)
  for (const sub of [...AGENT_DIRS, ...MODE_DIRS]) {
    watchDir(path.join(state.dir, sub), isMarkdownEvent)
  }

  return {}
}

export default ConfigReload
