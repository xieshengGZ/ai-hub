# 架构

## 组件

| 组件 | 位置 | 职责 |
|---|---|---|
| **Hub** | 一台服务器 | HTTP 服务 + SQLite + 静态前端；维护账号、agent、会话、任务、文件；给 Daemon 下发任务 |
| **Daemon** | 每台被托管的机器 | 长轮询领任务、为每个 agent 拉起一个 `kimi acp` 子进程、流式回传、显式回传文件、心跳、本地记账 |
| **Web / Android / iOS** | 浏览器 / PWA | 同一套原生 JS 单页应用，无构建步骤；桌面浏览器直接用，Android 与 iOS 通过 PWA 安装到主屏幕；和 Hub 走同源 JSON 接口 |

Hub 不保存模型密钥；Daemon 不保存业务数据。两边唯一的凭据是 **host token**。

## 数据模型（SQLite）

```
agents      id, name, host, cwd, model, prompt, always_on, managed_by, deleted, status
hosts       name, token, created, last_seen
messages    id, agent, role(me|agent), text, ts, status(running|done|error|canceled), job, mode
jobs        id, agent, user_msg, agent_msg, status(queued|running|done|error|canceled),
            claimed, started, ended, reply, error
files       id, name, size, mime, agent, kind(in|out), path, ts, msg, stored
skills      id, name, summary, when_to_use, body, version
agent_skills agent, skill, enabled
usage_daily agent, day, jobs, ms
```

- `messages.text` 是流式累加的；`jobs.agent_msg` 指向这轮回答那条消息。
- 附件（`kind=in`）在任务被领取时把 `msg` 写成本轮用户消息 id，于是能挂回气泡；产出（`kind=out`）由 `aihub-send` 上传时挂到最后一条 agent 消息下面。

## 一次任务的时序

```
浏览器            Hub                      Daemon                kimi acp
  │  POST /api/send │                          │                     │
  ├────────────────>│ 建 user_msg/agent_msg/job │                     │
  │                 │ status=queued            │                     │
  │                 │<───── 长轮询 /daemon/poll ─┤                     │
  │                 ├──── job + agent + files ─>│                     │
  │                 │                          ├── session/prompt ──>│
  │                 │<──── POST /daemon/event ──┤<── 流式 chunk ──────┤
  │  GET /api/msg   │  text 累加                │                     │
  │<────────────────┤                          │                     │
  │                 │<──── kind=done ──────────┤ aihub-send → /daemon/send_file
```

- 领任务分两段：先 `claimed`，Daemon 回 `kind=start` 才置 `running`。这样长轮询响应丢失也不会让任务永远 running。
- Daemon 启动时调 `/api/daemon/recover` 回收遗留任务。

## 文件与目录

```
<data>/
  aihub.db                 SQLite
  files/<id>               产出二进制
  inbox/<agent>/<YYYYMMDD>/<fid>_<原名>   用户上传的附件
  inbox/uploads.jsonl      上传流水（time/agent/id/name/size/mime/path）
<agent cwd>/
  AGENTS.md                Daemon 每轮写入的人格 + 发文件说明（不含技能清单）
  skills/<skill>           绑定到该 agent 的技能，软链到 ~/aihub/skills/<skill>
  .kimi-home/              选中中枢模型时 Daemon 写入的 Kimi 配置
  .aihub-inbox/            本轮附件落地位置（相对路径写进提示词）
```

Daemon 启动 `kimi` 时传 `--skills-dir <agent cwd>/skills`，Kimi 只认这个目录；本机共享库是 `~/aihub/skills/`，安装/卸载技能就是建/删软链。Hub 的 `skills` 表是心跳上报的缓存，不是源。

## 为什么 Daemon 只做出站连接

被托管的机器大多在内网、没有公网入口，或者出向被白名单限制。全部由 Daemon 主动连 Hub，
Hub 不需要任何入站规则，也不需要在防火墙里给每台机器开口子。

## 为什么用长轮询而不是 WebSocket

Hub 只有几百行、单进程、SQLite，长轮询足够（20 秒一轮），断线重连天然可靠，
不需要维护连接状态。任务量大的时候再换 WebSocket 也不影响协议形状。
