# Project map

Read this before changing Hub or Daemon behavior. It is the shortest path to the moving parts; keep it accurate when behavior changes.

## Shape

```
Browser / PWA ── HTTPS ──> Hub (hub/hub.py) ── outbound long poll ──> Daemon (daemon/daemon.py)
                              │                                            │
                              └── SQLite + files/ + inbox/                 └── stdio JSON-RPC ──> kimi acp
```

- **Hub**: HTTP, cookie auth, SQLite, static files, file storage. It never connects to a Daemon.
- **Daemon**: runs on each agent host, one process per machine, one `kimi acp` child per agent.
- **Web**: `hub/static/app.js`, native JS with no build step; same-origin JSON and SSE.

## Main request path

1. `POST /api/send` creates the user message, an empty agent message, and a `queued` job.
2. `GET /api/daemon/poll` long-polls, marks the job `claimed`, and returns the job, agent config, files, and enabled skills.
3. `POST /api/daemon/event` with `kind=start` moves the job to `running`.
4. `kind=chunk` appends text to the agent message; `kind=done` closes the job; `kind=error` records the failure.
5. Attachments are bound to the user message at claim time; replies read them from `.aihub-inbox/`.

The two-phase claim (`claimed`, then `start`) exists so a lost poll response cannot leave a job running forever. `sweep_stale` closes jobs that stay running too long.

## Where to look

| Concern | Start at |
|---|---|
| Message/job state machine | `hub/hub.py`: `session_send`, `daemon_poll`, `daemon_event`, `sweep_stale` |
| Auth and sessions | `hub/hub.py`: `session_new`, `session_ok`, `cookie_value`, TOTP helpers |
| ACP process per agent | `daemon/daemon.py`: `Acp`, `prompt_stream`, `ensure`, `kill` |
| One job end to end | `daemon/daemon.py`: `_run_job` |
| Files in and out | `hub/hub.py`: `upload`, `_file`, `artifact_add`; daemon: `fetch_file`, `cli_send` |
| Skills | `daemon/daemon.py`: `skill_store`, `link_skill`, `heartbeat`; Hub: `/api/skills`, `daemon_hb` |
| Frontend state | `hub/static/app.js`: `boot`, `pull`, `draw`, `send`, `loadSkills` |

## Skills

- The source of truth is the host's `~/aihub/skills/<id>/SKILL.md`.
- Installing for an agent creates a symlink at `<agent cwd>/skills/<id>`; uninstalling removes it.
- ACP ignores `--skills-dir`; it scans `$KIMI_CODE_HOME/skills` (that is `<agent cwd>/.kimi-home/skills`). `materialize_skills` mirrors the bound skills there via `sync_home_skills` on every job, so binding or unbinding takes effect without restarting the ACP process; the catalog is re-read per prompt.
- The Hub's `skills` table is a cache fed by heartbeat (`skill_store`, `skill_installs`), not the source.
- A chat message starting with `/<skill-id>` is translated into a skill instruction in `_run_job`; it reads `skills/<id>/SKILL.md` and does not depend on the ACP skill catalog.

## Gotchas

- `AGENTS.md` in an agent's cwd is rewritten every job. Do not put durable instructions there.
- One job per agent: a second `/api/send` while busy returns 409.
- A running job is swept to `error` after about 20 minutes.
- Single password equals full access; the Daemon runs agents with its own user's privileges.
- The project has no unit tests. CI runs syntax checks, a Hub smoke test, and `tests/hub_flow.py`.
