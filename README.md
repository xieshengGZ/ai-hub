# 企业 AI 中枢 · AI Hub

[![ci](https://github.com/xieshengGZ/ai-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/xieshengGZ/ai-hub/actions/workflows/ci.yml)

把散落在多台机器上的 AI agent（默认基于 Kimi CLI 的 ACP 协议）收拢成一个 Web 工作台：
在手机或浏览器里给每台机器派活、看流式输出、收产出文件、传附件。

> 一个 Hub + 每台机器一个 Daemon。Hub 不存模型密钥，Daemon 不存业务数据；
> 任务在机器本地执行，Hub 只做消息与状态的中转。

## 特性

- **多主机**：一台机器装一个 Daemon，Hub 自动发现、心跳、断线重连。
- **一对一会话**：每个 agent 一条会话，流式输出，随时中止。
- **团队**：指定一个 agent 做主 agent，把其他 agent 编入它的团队；主 agent 用 `dispatch` 给成员派活、用 `task` 取回结论，团队外的 agent 会被拒绝。
- **输出模式**：完整 / 仅阶段 / 仅结果，三种视角切换，不重跑任务。
- **附件**：拍照 / 相册 / 文件三种入口，落盘到 `inbox/<agent>/<日期>/`，发消息时把未取走的附件（最多 8 个，正文提到的优先）带给 agent；图片 agent 用 `ReadMediaFile` 真读。
- **预览**：图片、PDF、音视频、文本 / Markdown / 代码，点开即看，也能下载。
- **Markdown**：标题、列表、表格、代码块、引用，聊天与文档预览都按 Markdown 渲染。
- **技能包**：给 agent 绑定 `SKILL.md`，Daemon 从本机共享库 `~/aihub/skills/` 软链到工作目录的 `skills/`，再镜像进 agent 的 `KIMI_CODE_HOME/skills`（ACP 实际扫描的目录）；`AGENTS.md` 只写人格与发文件说明。
- **文件回传**：agent 显式执行 `aihub-send.sh` / `aihub-send.cmd` 才上传，挂在那条回复下面；也能 `--to <agent>` 投给别的 agent。
- **一键接入**：Linux 一行 `curl ... | bash`，Windows 一行 PowerShell，自动注册服务/计划任务。
- **PWA**：手机添加到主屏幕，像原生应用一样用。

## 架构

```
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ Web 浏览器     │  │ Android        │  │ iOS            │
│ 桌面 / PWA     │  │ PWA            │  │ PWA            │
└───────┬────────┘  └───────┬────────┘  └───────┬────────┘
        └───────────────────┼───────────────────┘
                            │ HTTPS（同源 JSON / SSE）
                            ▼
        ┌─────────────────────────────┐
        │  Hub（一台服务器）           │
        │  Python + SQLite + 静态页    │
        │  · 账号 / 会话 / 任务 / 文件  │
        │  · 长轮询下发任务            │
        └──────────┬──────────────────┘
                   │ HTTPS（出站轮询 / 心跳）
     ┌─────────────┼──────────────┐
     │             │              │
┌────▼────┐  ┌─────▼─────┐  ┌─────▼─────┐
│ Daemon  │  │  Daemon   │  │  Daemon   │
│ 机器 A  │  │  机器 B    │  │  机器 C    │
│ kimi acp│  │  kimi acp │  │  kimi acp │
└─────────┘  └───────────┘  └───────────┘
```

- Hub 从不主动连 Daemon，所有连接都是 Daemon 出站，内网机器也能接。
- 每个 agent 在工作目录里跑一个 `kimi acp` 子进程，Hub 与它之间是 stdio JSON-RPC。
- Daemon 只认 host token；Hub 不保存任何模型的 API key。

## 快速开始

### 1. Hub

需要 Python 3.10+（只用标准库，无第三方依赖）。

```bash
sudo useradd -m -s /bin/bash aihub
sudo mkdir -p /opt/ai-hub && sudo chown -R aihub:aihub /opt/ai-hub
# 把本仓库放到 /opt/ai-hub
sudo -u aihub cp hub/config.example.json hub/config.json
sudo -u aihub python3 hub/hub.py        # 先前台跑一次，确认能起
```

`hub/config.json`：

| 字段 | 说明 |
|---|---|
| `password` | 控制台登录口令，务必改掉 `change-me` |
| `data` | 数据目录（SQLite、附件、产出），默认 `~/aihub/data` |
| `port` | 本机监听端口，默认 `8791`（建议只监听 127.0.0.1，前面放 nginx） |
| `public_url` | 对外地址，如 `https://example.com/hub`；留空则按请求头推导（用于生成接入脚本） |
| `secret` | 可选。会话 cookie 的签名密钥；不填则首次启动自动生成到 `<data>/secret`（600） |

用 nginx 反代（见 `deploy/nginx.conf.example`），再用 systemd 托管（`deploy/aihub-hub.service`）：

```bash
sudo cp deploy/aihub-hub.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aihub-hub
```

打开 `https://<你的域名>/hub/`，用 `password` 登录。

### 2. 接入一台机器

控制台 → 主机 → 新建（例如 `my-box`），会给出两条命令，按机器的系统选一条：

Linux：

```bash
curl -fsSL "https://<域名>/hub/api/host/install.sh?name=my-box&t=<token>" | bash
```

Windows（PowerShell）：

```powershell
iwr -useb "https://<域名>/hub/api/host/install.ps1?name=my-box&t=<token>" | iex
```

脚本会做四件事：把 `daemon.py` 放到 `~/aihub/daemon/`、写 `config.json`、注册为服务
（Linux `systemd`，Windows 计划任务，登录自启）、立即启动。

前置条件：该机器有 **Python 3.10+**，且 `kimi` 在 PATH 里（Daemon 靠它拉起 agent）。

### 3. 建 agent

控制台 → 新建：

- **主机**：选刚接入的那台；
- **工作目录**：留空会自动建在 `~/aihub/agents/<id>`；
- **系统提示词**：写清楚它是谁、怎么回答；
- 需要的话勾「常驻」。

建完点进去就能对话。附件、预览、Markdown 都是现成的。

## 目录结构

```
hub/
  hub.py              # 全部后端：HTTP、SQLite、静态、接入脚本
  static/             # 前端（原生 JS，无框架、无构建）
daemon/
  daemon.py           # 每台机器一个：领任务、跑 kimi acp、回传
deploy/               # systemd 与 nginx 示例
docs/                 # 架构、协议、安全
```

## 安全

- 默认是**单口令**登录，口令即全权。公网部署务必：HTTPS、强口令、限制来源 IP。
- Daemon 以运行用户的身份执行 agent，**agent 能做的事等于该用户能做的事**。别在有敏感数据的机器上用高权限账号跑。
- 附件只会落在 Hub 数据目录与 agent 工作目录，但 agent 本身可以读写它所在机器。
- 详见 `docs/security.md`。

## 已知限制

- 单用户单口令，没有多租户与角色；
- 会话与消息保留 180 天，产出 30 天，附件 7 天（可在 `cleanup_loop` 里改）；
- 前端不做构建、不打 bundle，图省事，不做极致优化；
- 只实现了 Kimi CLI 的 ACP 适配，换别的 agent 需要改 `daemon.py` 里的启动方式。

## License

MIT，见 `LICENSE`。
