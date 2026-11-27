# 接口

所有接口都在 Hub 的 `/api/` 下。浏览器侧用 cookie 会话；Daemon 侧用 `X-Daemon: <host token>` 头。

## 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | `{password}` → 下发 `sid` cookie |
| POST | `/api/logout` | 清 cookie |
| GET | `/api/health` | 存活探测，返回 `{ok, ts}` |

## 浏览器（cookie）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/bootstrap` | 首屏：agent 列表 + 服务端时间 |
| GET | `/api/events` | SSE，数据变更版本号；前端据此刷新列表 |
| GET | `/api/msg?agent=&after=` | 拉消息（增量）+ 当前任务 + agent 状态；每条消息带 `files[]` |
| POST | `/api/send` | `{agent, text, mode}` 建任务 |
| POST | `/api/cancel` | `{agent}` 中止当前任务 |
| GET | `/api/tasks` / `POST /api/task/retry` | 任务列表 / 重试 |
| GET | `/api/usage` | 按 agent/天的用量 |
| GET | `/api/artifacts?agent=` | agent 的产出列表 |
| GET | `/api/files?agent=` | 暂存附件（未取走的） |
| POST | `/api/upload?agent=` | 原始字节 + `X-Filename`（URL 编码） |
| GET | `/api/file?id=&dl=` | 读文件；`dl=1` 走 attachment |
| POST | `/api/file/delete` | `{id}` 删除暂存附件 |
| GET/POST | `/api/skills` `/api/skill/create` `/api/skill/bind` `/api/skill/delete` | 技能包 |
| GET/POST | `/api/hosts` `/api/host/create` `/api/host/token` `/api/host/rotate` `/api/host/delete` | 主机与 token |
| GET | `/api/host/install.sh?name=&t=` | Linux 接入脚本 |
| GET | `/api/host/install.ps1?name=&t=` | Windows 接入脚本 |
| GET | `/api/host/daemon.py?name=&t=` | daemon 源码（凭 host token） |
| GET/POST | `/api/team` `/api/team/add` `/api/team/remove` | 主 agent 与成员关系 |
| POST | `/api/agent/create` `/api/agent/delete` `/api/agent/restore` `/api/agent/purge` `/api/agent/always_on` | agent 生命周期 |
| POST | `/api/agent/pin` | `{agent, on}` 置顶 / 取消置顶会话 |
| POST | `/api/agent/reload` | 重启该 agent 的 ACP 进程，重载技能与配置；会话历史保留 |
| GET | `/api/search?q=` | 搜消息与文件名 |

## Daemon（`X-Daemon`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/daemon/poll?wait=20` | 长轮询领任务；返回 `{job, agent, files}` |
| POST | `/api/daemon/event` | `{job, kind: start|chunk|done|error|canceled, text}` |
| POST | `/api/daemon/dispatch` | 主 agent 派活：`{agent, text}` |
| GET | `/api/daemon/task?id=` | 查任务结论 |
| GET | `/api/daemon/agents` | 本机 agent 配置（含技能、提示词） |
| POST | `/api/daemon/hb` | 心跳：`{host, agents:[{id,state}]}` |
| POST | `/api/daemon/recover` | 启动时回收遗留任务 |
| POST | `/api/daemon/send_file` | 显式回传文件：原始 body + `X-Filename`/`X-From`/`X-To`；`X-To` 为空就挂到最后一条回复下 |
| POST | `/api/daemon/artifact` | 旧的产出元信息入口（逐轮扫描已停用）；回传用 `send_file` |
| POST | `/api/daemon/upload` | 旧的产出二进制入口，配合 `artifact` 使用 |
| POST | `/api/daemon/usage` | 上报按天的本地用时 |

## Daemon ↔ kimi

Daemon 与 `kimi acp` 之间是 stdio 上的 JSON-RPC（ACP 协议）：

- `initialize` → `session/new` 或 `session/load`
- `session/prompt` 的 `prompt` 是内容块数组：`[{type:"text", text}]`
- 流式结果通过 `session/update` 通知，`sessionUpdate=agent_message_chunk` 里取 `content.text`
- 模型需要读文件时，Daemon 通过 `fs/read_text_file` 提供服务

提示词里的附件以本机路径给出；图片额外提示 agent 用 `ReadMediaFile` 打开真实内容。
