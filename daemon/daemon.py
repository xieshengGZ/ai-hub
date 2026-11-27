#!/usr/bin/env python3
"""AI 中枢 · Daemon（每台机器一个）

向 Hub 长轮询领任务；为每个 agent 拉起一个 `kimi acp` 子进程；流式回传；本地记账；
心跳；产出扫描与上传。不保存任何模型密钥。
"""
import fnmatch, hashlib, json, mimetypes, os, queue, re, shutil, socket, subprocess, sys, threading, time, urllib.parse, urllib.request

CONF = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), encoding="utf-8"))
HUB = CONF["hub"].rstrip("/")
TOKEN = CONF["token"]
NAME = CONF.get("name") or "未命名主机"
LOG_DIR = os.path.expanduser(CONF.get("log_dir", "~/aihub/logs"))
os.makedirs(LOG_DIR, exist_ok=True)
STORE = os.path.join(os.path.expanduser("~"), "aihub", "skills")
os.makedirs(STORE, exist_ok=True)

# pythonw（Windows 计划任务常用）没有控制台，stdout/stderr 是 None，
# 任何 print / sys.stderr.write 都会直接抛异常，所以先兜住。
if sys.stderr is None or sys.stdout is None:
    _p = os.path.join(LOG_DIR, "daemon.log")
    _f = open(_p, "a", buffering=1, encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = _f
    if sys.stdout is None:
        sys.stdout = _f

VERSION = 2026112705   # 自更新只升不降：中枢那份比我旧就跳过

PROCS, LOCK, STOP = {}, threading.Lock(), threading.Event()
ACTIVE, KNOWN, WHERES = set(), set(), {}
RUNNING_JOBS = set()
LOCK_SOCK = None
KIMI_CONF = (CONF.get("kimi") or "").strip()


def resolve_cwd(agent):
    """cwd 留空就用本机的 ~/aihub/agents/<id>（Windows / Linux 都对）。"""
    c = (agent.get("cwd") or "").strip()
    if not c:
        c = "~/aihub/agents/%s" % (agent.get("id") or "agent")
    return os.path.abspath(os.path.expanduser(c))


def kimi_argv():
    """Windows 上 kimi 可能是 .cmd/.bat，需要经 cmd /c 才能拉起。"""
    exe = KIMI_CONF or shutil.which("kimi") or "kimi"
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe]
    return [exe]


def py_exe():
    """daemon 自己在用的解释器；Windows 上 pythonw 没控制台，优先换成 python。"""
    exe = sys.executable or "python"
    if os.name == "nt" and exe.lower().endswith("pythonw.exe"):
        cand = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(cand):
            return cand
    return exe


def http(path, data=None, headers=None, raw=False, timeout=60):
    body, h = None, {"X-Daemon": TOKEN}
    if headers:
        h.update(headers)
    if raw:
        body = data
    elif data is not None:
        body = json.dumps(data, ensure_ascii=False).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(HUB + path, data=body, headers=h,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = r.read()
    return out if raw else json.loads(out.decode() or "{}")


SEND_SH = r"""#!/usr/bin/env bash
# 发文件（不依赖 python，一次 POST 完成）：
#   ./aihub-send.sh <文件>                挂到当前回复下面给用户
#   ./aihub-send.sh <文件> --to <agent>   投递到另一个 agent 的暂存区
set -euo pipefail
f=""; to=""
while [ $# -gt 0 ]; do
  case "$1" in
    --to) to="${2:?--to 后面要跟 agent id}"; shift 2;;
    *) f="$1"; shift;;
  esac
done
if [ -z "$f" ] || [ ! -f "$f" ]; then echo "用法: ./aihub-send.sh <文件> [--to <agent>]"; exit 2; fi
name=$(basename "$f")
size=$(wc -c <"$f" | tr -d ' ')
enc=$(printf '%s' "$name" | od -An -tx1 | tr -d ' \n' | sed 's/../%&/g')
resp=$(curl -fsS -X POST "$AIHUB_HUB/api/daemon/send_file" \
  -H "X-Daemon: $AIHUB_TOKEN" \
  -H "X-Filename: $enc" \
  -H "X-From: $AIHUB_AGENT" \
  -H "X-To: $to" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @"$f")
case "$resp" in
  *'"ok": true'*|*'"ok":true'*) ;;
  *) echo "上传失败: $resp"; exit 1;;
esac
printf '%s/%s\n' "$(cd "$(dirname "$f")" && pwd)" "$name" >> "$(dirname "$0")/.aihub-sent" 2>/dev/null || true
if [ -n "$to" ]; then echo "已投递给 $to：$name（$size 字节）"; else echo "已发送给用户：$name（$size 字节）"; fi
"""

# Windows 版走 daemon 自己的解释器，避免 cmd 的代码页把中文文件名搞乱。
SEND_CMD = r"""@echo off
setlocal
"@PY@" "@DAEMON@" send @AGENT@ %*
"""


class Acp:
    def __init__(self, agent):
        self.agent, self.p, self.sid = agent, None, None
        self.seq, self.lock = 1, threading.Lock()
        self.wlock = threading.Lock()
        self.pending, self.notify = {}, queue.Queue()
        self.last_used = time.time()
        self.skills_sig = ""
        self.model_sig = None
        cwd = resolve_cwd(agent)
        agent["cwd"] = cwd
        os.makedirs(cwd, exist_ok=True)

    def materialize_skills(self):
        """写 AGENTS.md 的人格，并把绑定的技能镜像进 kimi 实际扫描的目录。"""
        cwd = self.agent["cwd"]
        os.makedirs(cwd, exist_ok=True)
        lines = ["# %s\n\n%s\n" % (self.agent.get("name", ""), self.agent.get("prompt", "")),
                 "\n## 给用户发文件\n\n**系统不会自动回收工作目录里的文件**，只有你显式执行脚本，\n"
                 "文件才会出现在聊天里。所以：\n\n"
                 "- 用户明确要文件（「把这个发给我」「把结果发我」）→ 在本目录执行：\n\n"
                 "      ./aihub-send.sh <文件路径>        （bash / Git Bash）\n"
                 "      aihub-send.cmd <文件路径>        （cmd / PowerShell）\n\n"
                 "- 用户没提，就只回文字，不要主动上传。\n"
                 "- 脚本会把文件挂在你这条回复下面，用户能直接下载；\n"
                 "  要发给另一个 agent，加 `--to <agent_id>`，文件进对方暂存区。\n"
                 "\n"]
        with open(os.path.join(cwd, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("".join(lines))
        try:
            send_sh = os.path.join(cwd, "aihub-send.sh")
            with open(send_sh, "w", encoding="utf-8", newline="\n") as f:
                f.write(SEND_SH)
            os.chmod(send_sh, 0o755)
            send_cmd = os.path.join(cwd, "aihub-send.cmd")
            with open(send_cmd, "w", encoding="ascii", newline="\r\n") as f:
                f.write(SEND_CMD.replace("@PY@", py_exe())
                        .replace("@DAEMON@", os.path.abspath(__file__))
                        .replace("@AGENT@", self.agent["id"]))
        except OSError as e:
            sys.stderr.write("write aihub-send failed: %r\n" % e)
        try:
            home, _ = self._ensure_model_home()
            sync_home_skills(home, cwd)
        except Exception as e:
            sys.stderr.write("skills mirror failed: %r\n" % e)

    def _ensure_model_home(self):
        """选了中枢模型：写 <cwd>/.kimi-home 并用它；没选：沿用机器默认配置。"""
        cfg = self.agent.get("model_cfg") or None
        if not cfg:
            return None, []
        home = os.path.join(self.agent["cwd"], ".kimi-home")
        os.makedirs(home, exist_ok=True)
        path = os.path.join(home, "config.toml")

        def esc(v):
            return str(v or "").replace("\\", "\\\\").replace('"', '\\"')

        toml = ('default_model = "hub"\n\n'
                '[providers.hub]\n'
                'type = "%s"\n'
                'base_url = "%s"\n'
                'api_key = "%s"\n\n'
                '[models.hub]\n'
                'provider = "hub"\n'
                'model = "%s"\n'
                'display_name = "%s"\n'
                'max_context_size = %d\n'
                'capabilities = [ "tool_use" ]\n') % (
            esc(cfg.get("kind") or "openai"), esc(cfg.get("base_url")),
            esc(cfg.get("api_key")), esc(cfg.get("model_id")),
            esc(cfg.get("name") or "hub"), int(cfg.get("max_context") or 131072))
        try:
            old_txt = open(path, encoding="utf-8").read()
        except OSError:
            old_txt = ""
        if old_txt != toml:
            with open(path, "w", encoding="utf-8") as f:
                f.write(toml)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        src_home = os.path.expanduser("~/.kimi-code")
        for fn in ("device_id", "region"):
            s_p, d_p = os.path.join(src_home, fn), os.path.join(home, fn)
            if os.path.isfile(s_p) and not os.path.exists(d_p):
                try:
                    shutil.copy2(s_p, d_p)
                except OSError:
                    pass
        return home, ["--model", "hub"]

    def ensure(self):
        self.materialize_skills()
        home0, margs0 = self._ensure_model_home()
        sys.stderr.write("[acp] ensure %s p=%r sid=%r\n" % (self.agent["id"], bool(self.p), self.sid)); sys.stderr.flush()
        if self.p and self.p.poll() is None and self.sid:
            return
        sys.stderr.write("[acp] spawn kimi acp\n"); sys.stderr.flush()
        kw = {}
        if os.name == "nt":
            # 父进程无控制台时会为控制台子进程新建一个窗口，这里禁掉
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kw["startupinfo"] = si
        sk_dir = os.path.join(self.agent["cwd"], "skills")
        os.makedirs(sk_dir, exist_ok=True)
        home, margs = home0, margs0
        env = dict(os.environ)
        env.update({"AIHUB_HUB": HUB, "AIHUB_TOKEN": TOKEN, "AIHUB_AGENT": self.agent.get("id", "")})
        if home:
            env["KIMI_CODE_HOME"] = home
        self.p = subprocess.Popen(kimi_argv() + margs + ["--skills-dir", sk_dir, "acp"],
                                  cwd=self.agent["cwd"], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                  encoding="utf-8", errors="replace", **kw)
        threading.Thread(target=self._reader, daemon=True).start()
        sys.stderr.write("[acp] initialize\n"); sys.stderr.flush()
        r = self.call("initialize", {"protocolVersion": 1,
                                     "clientCapabilities": {"fs": {"readTextFile": True,
                                                                   "writeTextFile": True},
                                                            "terminal": False}})
        opts = {o.get("id"): o for o in (r.get("configOptions") or []) if isinstance(o, dict)}
        model = (self.agent.get("model") or "").strip()
        if model and "model" in opts:
            if model in [x.get("value") for x in opts["model"].get("options", [])]:
                try:
                    self.call("session/set_config_option",
                              {"sessionId": None, "optionId": "model", "value": model})
                except Exception as e:
                    sys.stderr.write("set model failed: %r\n" % e)
        sid_file = os.path.join(LOG_DIR, "sid-%s.txt" % self.agent["id"])
        sid = ""
        if os.path.exists(sid_file):
            sid = open(sid_file, encoding="utf-8").read().strip()
        if sid:
            try:
                self.call("session/load", {"sessionId": sid, "cwd": self.agent["cwd"], "mcpServers": []})
                self.sid = sid
                return
            except Exception as e:
                sys.stderr.write("session/load failed (%s), 新建会话\n" % str(e)[:80])
        self.sid = self.call("session/new", {"cwd": self.agent["cwd"], "mcpServers": []})["sessionId"]
        open(sid_file, "w", encoding="utf-8").write(self.sid)

    def _write(self, obj):
        with self.wlock:
            try:
                self.p.stdin.write(json.dumps(obj) + "\n")
                self.p.stdin.flush()
            except Exception as e:
                sys.stderr.write("write to acp failed: %r\n" % e)

    def _handle_request(self, msg):
        """处理 agent 发来的请求：权限与文件读写。"""
        mid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") or {}
        try:
            if method == "session/request_permission":
                opts = params.get("options") or []
                pick = None
                for o in opts:
                    if str(o.get("kind", "")).startswith("allow"):
                        pick = o
                        if o.get("kind") == "allow_always":
                            break
                if pick:
                    self._write({"jsonrpc": "2.0", "id": mid,
                                 "result": {"outcome": {"outcome": "selected",
                                                        "optionId": pick.get("optionId")}}})
                else:
                    self._write({"jsonrpc": "2.0", "id": mid,
                                 "result": {"outcome": {"outcome": "cancelled"}}})
                return
            if method == "fs/read_text_file":
                path = params.get("path")
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    data = f.read()
                self._write({"jsonrpc": "2.0", "id": mid, "result": {"content": data}})
                return
            if method == "fs/write_text_file":
                path = params.get("path")
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(params.get("content") or "")
                self._write({"jsonrpc": "2.0", "id": mid, "result": {}})
                return
            self._write({"jsonrpc": "2.0", "id": mid,
                         "error": {"code": -32601, "message": "unsupported: %s" % method}})
        except Exception as e:
            self._write({"jsonrpc": "2.0", "id": mid,
                         "error": {"code": -32603, "message": str(e)[:200]}})

    def _reader(self):
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if "id" in msg and "method" in msg:
                self._handle_request(msg)
            elif "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"], None).put(msg)
            elif msg.get("method") == "session/update":
                self.notify.put(msg)

    def call(self, method, params, timeout=300):
        if not self.p or self.p.poll() is not None:
            raise RuntimeError("acp 进程不在了")
        with self.lock:
            mid, self.seq = self.seq, self.seq + 1
        q = queue.Queue()
        self.pending[mid] = q
        self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        finally:
            self.pending.pop(mid, None)
        if "error" in msg:
            raise RuntimeError(json.dumps(msg["error"], ensure_ascii=False)[:300])
        return msg.get("result") or {}

    def prompt_stream(self, parts, on_chunk, is_canceled, on_activity=None):
        """发一轮提示，边收边回调。返回 stopReason 或 'canceled'。"""
        self.last_used = time.time()
        self.skills_sig = ""
        self.ensure()
        # 关键：清掉上一个任务残留的通知，否则旧文本会被算进新回答
        while True:
            try:
                self.notify.get_nowait()
            except queue.Empty:
                break
        with self.lock:
            mid, self.seq = self.seq, self.seq + 1
        done = queue.Queue()
        self.pending[mid] = done
        self._write({"jsonrpc": "2.0", "id": mid, "method": "session/prompt",
                     "params": {"sessionId": self.sid, "prompt": parts}})
        while True:
            try:
                msg = done.get_nowait()
                # 收尾：响应可能先于最后几个 chunk 到达，把队列里剩下的吐完再返回，
                # 否则长回复的尾巴会被丢掉（表现为截断在半句话）。
                while True:
                    try:
                        n = self.notify.get_nowait()
                    except queue.Empty:
                        break
                    upd = (n.get("params") or {}).get("update") or {}
                    if upd.get("sessionUpdate") == "agent_message_chunk":
                        t = (upd.get("content") or {}).get("text") or ""
                        if t:
                            on_chunk(t)
                if "error" in msg:
                    raise RuntimeError(json.dumps(msg["error"], ensure_ascii=False)[:300])
                return (msg.get("result") or {}).get("stopReason", "end_turn")
            except queue.Empty:
                pass
            if is_canceled():
                self.kill()
                return "canceled"
            try:
                msg = self.notify.get(timeout=0.4)
            except queue.Empty:
                on_chunk(None)
                continue
            upd = (msg.get("params") or {}).get("update") or {}
            if on_activity:
                try:
                    on_activity()
                except Exception:
                    pass
            if upd.get("sessionUpdate") == "agent_message_chunk":
                t = (upd.get("content") or {}).get("text") or ""
                if t:
                    on_chunk(t)

    def kill(self):
        try:
            if self.p and self.p.poll() is None:
                self.p.kill()
            if self.p:
                self.p.wait(timeout=2)
        except Exception:
            pass
        self.p, self.sid = None, None


def acp_for(agent):
    with LOCK:
        a = PROCS.get(agent["id"])
        if a is None:
            a = Acp(agent)
            PROCS[agent["id"]] = a
        else:
            agent["cwd"] = resolve_cwd(agent)
            a.agent = agent          # 每次任务刷新配置（提示词、技能）
        return a


def fetch_file(fid, name, cwd):
    inbox = os.path.join(cwd, ".aihub-inbox")
    os.makedirs(inbox, exist_ok=True)
    safe = "".join(ch for ch in name if ch not in "\\/\x00")[:80] or "file"
    dst = os.path.join(inbox, safe)
    with open(dst, "wb") as f:
        f.write(http("/api/file?id=%s" % fid, timeout=180, raw=True))
    return os.path.relpath(dst, cwd)


def run_job(job, agent, files):
    try:
        return _run_job(job, agent, files)
    except Exception as e:
        import traceback
        sys.stderr.write("job crashed: %r\n%s\n" % (e, traceback.format_exc()))
        try:
            http("/api/daemon/event", {"job": job.get("id"), "kind": "error",
                                       "error": ("daemon error: %s" % e)[:300]}, timeout=15)
        except Exception:
            pass


def _run_job(job, agent, files):
    agent["cwd"] = resolve_cwd(agent)   # Hub 上 cwd 留空时按 id 落盘，不能带着空串往下走
    jid, cwd, started = job["id"], agent["cwd"], time.time()
    with LOCK:
        if jid in RUNNING_JOBS:
            sys.stderr.write("skip duplicate job: %s\n" % jid)
            return
        RUNNING_JOBS.add(jid)
        ACTIVE.add(agent["id"])
    sys.stderr.write("[job] start %s agent=%s cwd=%s\n" % (jid, agent["id"], cwd)); sys.stderr.flush()
    try:
        ack = http("/api/daemon/event", {"job": jid, "kind": "start"}, timeout=15)
        if isinstance(ack, dict) and ack.get("cancel"):
            sys.stderr.write("server says cancel at start: %s\n" % jid)
            with LOCK:
                RUNNING_JOBS.discard(jid)
                ACTIVE.discard(agent["id"])
            return
    except Exception as e:
        sys.stderr.write("start ack failed: %r\n" % e)
    cwd = resolve_cwd(agent)
    os.makedirs(cwd, exist_ok=True)
    if not (agent.get("model_cfg") or {}).get("base_url"):
        raise RuntimeError("这个 agent 没有指定模型：请在中枢的会话里选一个模型后再发消息")
    text = job["text"]
    # 支持 "/技能id 任务描述" 的调用形式：翻译成明确的使用技能指令
    m = re.match(r"^/([a-z0-9_-]+)(?:\s+([\s\S]*))?$", (text or "").strip())
    if m:
        sid, rest = m.group(1), (m.group(2) or "").strip()
        sk = next((x for x in (agent.get("skills") or []) if x.get("id") == sid), None)
        if sk:
            text = ("请使用技能「%s」来完成下面的任务。适用场景：%s\n"
                    "技能正文在 skills/%s/SKILL.md，需要时读它再动手。\n\n任务：%s"
                    % (sk.get("name") or sid, (sk.get("when_to_use") or "")[:200], sid,
                       rest or "（用户没有补充说明，按技能默认流程执行）"))
    refs = []
    pics = []
    for f in files or []:
        try:
            rel = fetch_file(f["id"], f["name"], cwd)
            refs.append(rel)
            if f.get("kind") == "image":
                pics.append(rel)
        except Exception as e:
            sys.stderr.write("fetch failed: %r\n" % e)
    if refs:
        text += "\n\n参考文件（本机路径）：\n" + "\n".join("- " + r for r in refs)
    if pics:
        text += ("\n\n上面这些图片请先用 ReadMediaFile 打开看真实内容再回答，"
                 "不要凭文件名或大小猜。")
    parts = [{"type": "text", "text": text}]

    state = {"buf": [], "last": time.time(), "cancel": False, "stalled": False,
             "sent": 0, "last_text": time.time()}

    def push(force=False):
        chunk = "".join(state["buf"])
        if not chunk and not force:
            return
        state["buf"] = []
        state["last"] = time.time()
        try:
            r = http("/api/daemon/event", {"job": jid, "kind": "chunk", "text": chunk}, timeout=30)
            if r.get("cancel"):
                state["cancel"] = True
        except Exception as e:
            sys.stderr.write("chunk failed: %r\n" % e)

    def on_chunk(t):
        if t is None:
            if state["buf"] and time.time() - state["last"] > 0.8:
                push()
            elif time.time() - state["last"] > 2.0:
                push(force=True)      # 空 chunk 只为取回 cancel 标记
            if time.time() - state["last_text"] > 900:
                state["stalled"] = True
                state["cancel"] = True
            return
        state["buf"].append(t)
        state["last_text"] = time.time()
        state["sent"] += len(t)
        if sum(len(x) for x in state["buf"]) >= 120:
            push()

    sys.stderr.write("[job] spawn acp\n"); sys.stderr.flush()
    a = acp_for(agent)
    try:
        a.materialize_skills()   # 每轮都保证 AGENTS.md 与 aihub-send.sh 是最新的
    except Exception as e:
        sys.stderr.write("materialize failed: %r\n" % e)
    sys.stderr.write("[job] acp ready\n"); sys.stderr.flush()
    try:
        try:
            stop = a.prompt_stream(parts, on_chunk, lambda: state["cancel"],
                               lambda: state.__setitem__("last_text", time.time()))
        except RuntimeError as e:
            # CLI 侧会话被上一轮卡住（turn.agent_busy）或会话被别的进程改过（session.stale）：
            # 杀掉实例、丢掉旧 session，重开一次
            msg_ = str(e)
            if ("agent_busy" in msg_ or "already in progress" in msg_
                    or "session.stale" in msg_ or "updated by another client" in msg_):
                sys.stderr.write("session unusable (%s), restarting instance for %s\n"
                                 % (msg_[:40], agent["id"]))
                a.kill()
                a.sid = ""
                try:
                    os.remove(os.path.join(LOG_DIR, "sid-%s.txt" % agent["id"]))
                except OSError:
                    pass
                stop = a.prompt_stream(parts, on_chunk, lambda: state["cancel"],
                                   lambda: state.__setitem__("last_text", time.time()))
            else:
                raise
        push(force=True)
        if stop == "canceled" or state["cancel"]:
            http("/api/daemon/event", {"job": jid, "kind": "error", "error": "已中止"}, timeout=30)
        else:
            http("/api/daemon/event", {"job": jid, "kind": "done"}, timeout=30)
    except Exception as e:
        sys.stderr.write("job failed: %r\n" % e)
        push(force=True)
        http("/api/daemon/event", {"job": jid, "kind": "error", "error": str(e)[:300]}, timeout=30)

    with LOCK:
        RUNNING_JOBS.discard(jid)
        ACTIVE.discard(agent["id"])
    with open(os.path.join(LOG_DIR, "usage.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "agent": agent["id"], "job": jid,
                            "ms": int((time.time() - started) * 1000)}, ensure_ascii=False) + "\n")


def post_usage():
    """把本地账本按 agent/天 聚合成日结，上报中枢。"""
    path = os.path.join(LOG_DIR, "usage.jsonl")
    if not os.path.exists(path):
        return
    agg = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(r.get("ts", 0)))
            k = (r.get("agent"), day)
            a = agg.setdefault(k, {"jobs": 0, "ms": 0})
            a["jobs"] += 1
            a["ms"] += int(r.get("ms") or 0)
    for (agent, day), v in agg.items():
        try:
            http("/api/daemon/usage", {"agent": agent, "day": day, "jobs": v["jobs"], "ms": v["ms"]},
                 timeout=20)
        except Exception as e:
            sys.stderr.write("usage post failed: %r\n" % e)


def _parse_skill(path, sid):
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    name, summary, when = sid, "", ""
    for line in txt.splitlines()[:30]:
        t = line.strip()
        if t.startswith("name:") and len(t) > 5:
            name = t[5:].strip()
        elif t.startswith("description:"):
            summary = t[12:].strip()
        elif t.startswith("whenToUse:"):
            when = t[10:].strip()
    return {"id": sid, "name": name, "summary": summary, "when_to_use": when}


def skill_store():
    """本机共享技能库：~/aihub/skills/<id>/SKILL.md"""
    out = []
    if not os.path.isdir(STORE):
        return out
    for d in sorted(os.listdir(STORE)):
        p = os.path.join(STORE, d, "SKILL.md")
        if os.path.isfile(p):
            sk = _parse_skill(p, d)
            if sk:
                out.append(sk)
    return out


def skill_installs():
    """{agent: [已安装的 skill id]}，判定方式是链接指向共享库"""
    res = {}
    with LOCK:
        cwds = dict(WHERES)
    for aid, cwd in cwds.items():
        base = os.path.join(cwd or "", "skills")
        if not cwd or not os.path.isdir(base):
            continue
        got = []
        for d in os.listdir(base):
            p = os.path.join(base, d)
            try:
                if os.path.realpath(p) == os.path.realpath(os.path.join(STORE, d)):
                    got.append(d)
            except OSError:
                pass
        if got:
            res[aid] = sorted(got)
    return res


def link_skill(cwd, sid):
    src = os.path.join(STORE, sid)
    if not cwd or not sid:
        return "缺少 agent 目录或技能 id"
    if not os.path.isdir(src):
        return "共享库里没有这个技能"
    base = os.path.join(resolve_cwd({"id": "x", "cwd": cwd}), "skills")
    os.makedirs(base, exist_ok=True)
    dst = os.path.join(base, sid)
    if os.path.islink(dst) or os.path.isdir(dst):
        unlink_skill(cwd, sid)
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                           capture_output=True, text=True)
        return "ok" if r.returncode == 0 else ((r.stderr or r.stdout or "mklink 失败").strip()[:200])
    os.symlink(src, dst, target_is_directory=True)
    return "ok"


def unlink_skill(cwd, sid):
    dst = os.path.join(cwd or "", "skills", sid)
    if not (os.path.islink(dst) or os.path.isdir(dst)):
        return "没有安装"
    if os.path.islink(dst):
        os.unlink(dst)
    else:
        try:
            os.rmdir(dst)          # Windows junction：只删链接，不动目标
        except OSError:
            shutil.rmtree(dst, ignore_errors=True)
    return "ok"


def _link_dir(src, dst):
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout or "mklink failed").strip()[:200])
    else:
        os.symlink(src, dst, target_is_directory=True)


def _link_target(p):
    """软链或 junction 指向的目标；普通目录返回 None。"""
    try:
        return os.readlink(p)
    except OSError:
        return None


def _unlink_dir(p):
    try:
        os.unlink(p)
    except OSError:
        if os.name == "nt":
            os.rmdir(p)            # junction 只能 rmdir
        else:
            raise


def sync_home_skills(home, cwd):
    """ACP 只扫描 $KIMI_CODE_HOME/skills（--skills-dir 在 acp 下不生效），
    把 agent 绑定的技能镜像进去；每轮调用，跟上技能的绑定与解绑。"""
    if not home:
        return
    src, dst = os.path.join(cwd, "skills"), os.path.join(home, "skills")
    try:
        os.makedirs(dst, exist_ok=True)
    except OSError as e:
        sys.stderr.write("skills mirror: mkdir %s failed: %r\n" % (dst, e))
        return
    want = {}
    if os.path.isdir(src):
        for n in os.listdir(src):
            p = os.path.join(src, n)
            if os.path.isdir(p):
                want[n] = os.path.realpath(p)
    for n, target in want.items():
        p = os.path.join(dst, n)
        cur = _link_target(p)
        try:
            if cur is not None:
                abs_cur = cur if os.path.isabs(cur) else os.path.join(dst, cur)
                if os.path.realpath(abs_cur) == target:
                    continue
                _unlink_dir(p)
            elif os.path.exists(p):
                continue           # 用户自己放的真目录，不动
            _link_dir(target, p)
        except OSError as e:
            sys.stderr.write("skills mirror: link %s failed: %r\n" % (n, e))
    store, src_real = os.path.realpath(STORE) + os.sep, os.path.realpath(src) + os.sep
    for n in os.listdir(dst):
        if n in want:
            continue
        p = os.path.join(dst, n)
        tgt = _link_target(p)
        if tgt is None:
            continue               # 真目录，不是我们建的
        try:
            abs_tgt = tgt if os.path.isabs(tgt) else os.path.join(dst, tgt)
            real = os.path.realpath(abs_tgt)
            if (not os.path.exists(p) or real.startswith(store)
                    or real.startswith(src_real) or os.path.basename(real) == n):
                _unlink_dir(p)
        except OSError as e:
            sys.stderr.write("skills mirror: unlink %s failed: %r\n" % (n, e))


def _tombstones():
    try:
        with open(os.path.join(STORE, ".deleted"), encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def delete_store_skill(sid):
    sid = "".join(ch for ch in (sid or "") if ch not in "/\\\x00")
    p = os.path.join(STORE, sid)
    if not os.path.isdir(p):
        return "库里没有这个技能"
    shutil.rmtree(p, ignore_errors=True)
    try:
        with open(os.path.join(STORE, ".deleted"), "a", encoding="utf-8") as f:
            f.write(sid + "\n")
    except OSError:
        pass
    return "ok"


_AGENT_MIGRATED = [False]


def migrate_agent_skill_dirs():
    """把历史上直接拷进 agent 目录的 skills/<id>/ 收编：拷贝进共享库，再换成链接。"""
    if _AGENT_MIGRATED[0]:
        return
    with LOCK:
        cwds = dict(WHERES)
    if not cwds:
        return
    for aid, cwd in cwds.items():
        base = os.path.join(cwd or "", "skills")
        if not os.path.isdir(base):
            continue
        for d in os.listdir(base):
            p = os.path.join(base, d)
            if os.path.islink(p) or not os.path.isdir(p):
                continue
            if not os.path.isfile(os.path.join(p, "SKILL.md")):
                continue
            tgt = os.path.join(STORE, d)
            try:
                if not os.path.exists(tgt):
                    shutil.move(p, tgt)
                else:
                    shutil.rmtree(p, ignore_errors=True)
                link_skill(cwd, d)
                sys.stderr.write("adopted skill %s -> %s\n" % (d, aid))
            except OSError as e:
                sys.stderr.write("adopt %s failed: %r\n" % (d, e))
    _AGENT_MIGRATED[0] = True


_LAST_ADOPT = [0.0]


def adopt_agents_skills():
    """把 npx skills 之类工具装在 <cwd>/.agents/skills 下的技能收进共享库（复制，不动原目录）。"""
    if time.time() - _LAST_ADOPT[0] < 60:
        return
    _LAST_ADOPT[0] = time.time()
    with LOCK:
        cwds = dict(WHERES)
    for aid, cwd in cwds.items():
        base = os.path.join(cwd or "", ".agents", "skills")
        if not os.path.isdir(base):
            continue
        for d in os.listdir(base):
            src = os.path.join(base, d)
            if not os.path.isfile(os.path.join(src, "SKILL.md")):
                continue
            tgt = os.path.join(STORE, d)
            if os.path.exists(tgt) or d in _tombstones():
                continue
            try:
                shutil.copytree(src, tgt)
                sys.stderr.write("adopted .agents skill %s (from %s)\n" % (d, aid))
            except OSError as e:
                sys.stderr.write("adopt .agents %s failed: %r\n" % (d, e))


_LAST_USER_SYNC = [0.0]


def migrate_user_skills(force=False):
    """把 Kimi 用户级技能复制一份进共享库（不动原文件，个人 CLI 不受影响）。
    启动时跑一次，之后每 2 分钟补一次，新装的技能也能自动进库。"""
    if not force and time.time() - _LAST_USER_SYNC[0] < 120:
        return
    _LAST_USER_SYNC[0] = time.time()
    src = os.path.join(os.path.expanduser("~"), ".kimi-code", "skills")
    if not os.path.isdir(src):
        return
    for d in os.listdir(src):
        s_dir = os.path.join(src, d)
        t_dir = os.path.join(STORE, d)
        if d in _tombstones():
            continue
        if os.path.isfile(os.path.join(s_dir, "SKILL.md")) and not os.path.exists(t_dir):
            try:
                shutil.copytree(s_dir, t_dir)
                sys.stderr.write("migrated skill: %s\n" % d)
            except OSError as e:
                sys.stderr.write("migrate %s failed: %r\n" % (d, e))


# 进程启动时加载的这份代码的指纹：用来判断“磁盘上是新的、但跑的还是旧的”
RUNNING_HASH = hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()


def self_update():
    """从 Hub 拉最新 daemon.py：校验、热替换、原地重启。有任务在跑就等下一轮。"""
    with LOCK:
        if ACTIVE:
            return
    try:
        v = http("/api/daemon/version", timeout=15)
        cur = hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()
        if v.get("sha256") == cur and cur == RUNNING_HASH:
            return
        if v.get("sha256") == cur:
            sys.stderr.write("self-update: reloading new code\n")
            sys.stderr.flush()
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
            return
        data = http("/api/daemon/source", raw=True, timeout=60)
        mv = re.search(rb"^VERSION = (\d+)", data, re.M)
        remote_v = int(mv.group(1)) if mv else 0
        if remote_v and remote_v <= VERSION:
            sys.stderr.write("self-update: 中枢版本 %d 不比本地 %d 新，跳过\n" % (remote_v, VERSION))
            sys.stderr.flush()
            return
        new = os.path.abspath(__file__) + ".new"
        with open(new, "wb") as f:
            f.write(data)
        r = subprocess.run([sys.executable, "-m", "py_compile", new], capture_output=True)
        if r.returncode != 0:
            sys.stderr.write("self-update: new file does not compile, skip\n")
            os.remove(new)
            return
        shutil.copy2(os.path.abspath(__file__), os.path.abspath(__file__) + ".bak")
        os.replace(new, os.path.abspath(__file__))
        sys.stderr.write("self-update: applied %s, restarting\n" % (v.get("sha256") or "")[:12])
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    except Exception as e:
        sys.stderr.write("self-update failed: %r\n" % e)


def local_default_model():
    """读本机 ~/.kimi-code/config.toml 的默认模型，供中枢一次性接管老 agent。"""
    try:
        txt = open(os.path.expanduser("~/.kimi-code/config.toml"), encoding="utf-8").read()
    except OSError:
        return None
    alias = ""
    for line in txt.splitlines():
        if line.strip().startswith("default_model"):
            alias = line.split("=", 1)[1].strip().strip('"')
            break
    if not alias:
        return None
    mm = re.search(r"\[models\.%s\](.*?)(?=\n\[|\Z)" % re.escape(alias), txt, re.S)
    if not mm:
        return None
    body = mm.group(1)
    prov = re.search(r'provider\s*=\s*"([^"]+)"', body)
    mid = re.search(r'model\s*=\s*"([^"]+)"', body)
    mctx = re.search(r'max_context_size\s*=\s*(\d+)', body)
    if not prov or not mid:
        return None
    pm = re.search(r"\[providers\.%s\](.*?)(?=\n\[|\Z)" % re.escape(prov.group(1)), txt, re.S)
    if not pm:
        return None

    def get(k):
        x = re.search(r'%s\s*=\s*"([^"]*)"' % k, pm.group(1))
        return x.group(1) if x else ""

    return {"name": "%s 默认（%s）" % (NAME, alias), "base_url": get("base_url"),
            "api_key": get("api_key"), "model_id": mid.group(1),
            "kind": get("type") or "openai",
            "max_context": int(mctx.group(1)) if mctx else 131072}


def heartbeat():
    with LOCK:
        ids = set(KNOWN) | set(PROCS)
        agents = []
        for aid in ids:
            p = PROCS.get(aid)
            alive = bool(p and p.p and p.p.poll() is None)
            state = "busy" if aid in ACTIVE else ("on" if alive else "off")
            agents.append({"id": aid, "state": state})
    payload = {"agents": agents, "host": NAME,
               "skill_store": skill_store(), "skill_installs": skill_installs()}
    try:
        http("/api/daemon/hb", payload, timeout=15)
    except Exception as e:
        sys.stderr.write("hb failed: %r\n" % e)


ALWAYS = set()  # noqa


def refresh_agents():
    """常驻 agent 保持实例；非常驻的空闲 30 分钟回收。"""
    while not STOP.is_set():
        try:
            d = http("/api/daemon/agents", timeout=20)
            for a in d.get("agents", []):
                KNOWN.add(a["id"])
                WHERES[a["id"]] = a.get("cwd")
                if a.get("always_on"):
                    ALWAYS.add(a["id"])
                    if a["id"] not in PROCS:
                        a2 = {"id": a["id"], "name": a["name"], "cwd": a["cwd"],
                              "prompt": a.get("prompt") or "", "model": a.get("model") or ""}
                        acp_for(a2)
                        threading.Thread(target=PROCS[a["id"]].ensure, daemon=True).start()
                        sys.stderr.write("常驻实例拉起: %s\n" % a["id"])
            migrate_agent_skill_dirs()
            adopt_agents_skills()
            migrate_user_skills()
            for op in d.get("ops", []):
                try:
                    # 中枢让它重载：先把这条 op 标掉（否则新进程会再看到），杀掉所有会话，换新进程
                    if op.get("op") == "reload_self":
                        with LOCK:
                            idle = not ACTIVE
                        if not idle:
                            continue        # 有任务在跑：op 保持未完成，下一轮再来
                        http("/api/daemon/op_done", {"id": op["id"]}, timeout=15)
                        with LOCK:
                            insts = list(PROCS.values())
                            PROCS.clear()
                        for i_ in insts:
                            try:
                                i_.kill()
                            except Exception:
                                pass
                        sys.stderr.write("reload_self: 重启自己\n")
                        sys.stderr.flush()
                        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
                        continue
                    # 正在跑任务的 agent 不打断：把重启/清理类操作推迟到它空闲
                    if op.get("agent"):
                        with LOCK:
                            busy = op["agent"] in ACTIVE
                        if busy:
                            continue
                    a = PROCS.pop(op["agent"], None)
                    if a:
                        a.kill()
                    if op["op"] == "report_model":
                        dm = local_default_model()
                        if dm:
                            dm["host"] = NAME
                            try:
                                http("/api/daemon/model_report", dm, timeout=20)
                                sys.stderr.write("reported default model: %s\n" % dm.get("model_id"))
                            except Exception as e:
                                sys.stderr.write("report model failed: %r\n" % e)
                    if op["op"] in ("skill_install", "skill_uninstall", "skill_delete"):
                        sid = op.get("path") or ""
                        aid = op.get("agent") or ""
                        if op["op"] == "skill_install":
                            msg = link_skill(WHERES.get(aid) or "", sid)
                        elif op["op"] == "skill_uninstall":
                            msg = unlink_skill(WHERES.get(aid) or "", sid)
                        else:
                            msg = delete_store_skill(sid)
                        sys.stderr.write("skill op %s %s/%s: %s\n" % (op["op"], aid, sid, msg))
                        sys.stderr.flush()
                    if op["op"] == "purge_dir":
                        cwd = op.get("path") or WHERES.get(op["agent"])
                        if cwd and os.path.isdir(cwd) and cwd.startswith("/home/ubuntu/aihub/agents/"):
                            shutil.rmtree(cwd, ignore_errors=True)
                            sys.stderr.write("已清除目录: %s\n" % cwd)
                        else:
                            sys.stderr.write("未找到目录，agent=%s\n" % op["agent"])
                    http("/api/daemon/op_done", {"id": op["id"]}, timeout=15)
                except Exception as e:
                    sys.stderr.write("op failed: %r\n" % e)
            stale = [k for k in list(PROCS) if k not in KNOWN]
            for k in stale:
                sys.stderr.write("不属于本机，回收: %s\n" % k)
                PROCS.pop(k).kill()
            idle = [k for k, v in list(PROCS.items())
                    if k not in ALWAYS and v.p and v.p.poll() is None
                    and time.time() - v.last_used > 1800]
            for k in idle:
                sys.stderr.write("回收空闲实例: %s\n" % k)
                PROCS[k].kill()
        except Exception as e:
            sys.stderr.write("refresh agents failed: %r\n" % e)
        time.sleep(10)


def cli_send(agent, argv):
    """给 agent 用的发文件入口：daemon.py send <agent> <文件> [--to <agent>]"""
    f, to, i = "", "", 0
    while i < len(argv):
        a = argv[i]
        if a == "--to" and i + 1 < len(argv):
            to = argv[i + 1]
            i += 2
            continue
        if not f:
            f = a
        i += 1
    if not f or not os.path.isfile(f):
        print("用法: aihub-send <文件> [--to <agent>]")
        return 2
    name = os.path.basename(f)
    try:
        with open(f, "rb") as fh:
            blob = fh.read()
    except OSError as e:
        print("读不了这个文件: %r" % e)
        return 2
    if not blob or len(blob) > 120 * 1024 * 1024:
        print("文件为空或超过 120MB")
        return 2
    try:
        r = http("/api/daemon/send_file", blob, headers={
            "X-Filename": urllib.parse.quote(name),
            "X-From": agent, "X-To": to,
            "Content-Type": "application/octet-stream"}, raw=True, timeout=300)
        res = json.loads(r.decode("utf-8", "replace") or "{}")
    except Exception as e:
        print("上传失败: %r" % e)
        return 1
    if not res.get("ok"):
        print("上传失败: %s" % (res.get("error") or r[:200]))
        return 1
    try:
        with open(os.path.join(os.getcwd(), ".aihub-sent"), "a", encoding="utf-8") as fh:
            fh.write(os.path.abspath(f) + "\n")
    except OSError:
        pass
    if to:
        print("已投递给 %s：%s（%d 字节）" % (to, name, len(blob)))
    else:
        print("已发送给用户：%s（%d 字节）" % (name, len(blob)))
    return 0


def main():
    # 一台机器只允许一个 daemon：重复实例会各自拉起 acp，会话互相踩，报 session.stale。
    # 用本地端口占位当锁，进程退出即释放。
    global LOCK_SOCK
    try:
        s_ = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s_.bind(("127.0.0.1", 38017))
        s_.listen(1)
        LOCK_SOCK = s_
    except OSError:
        sys.stderr.write("已经有一个 daemon 在跑（127.0.0.1:38017 被占用），本进程退出\n")
        sys.stderr.flush()
        sys.exit(0)
    migrate_user_skills(force=True)
    print("daemon -> %s" % HUB, flush=True)
    try:
        http("/api/daemon/recover", {}, timeout=15)
        print("recovered stale jobs", flush=True)
    except Exception as e:
        sys.stderr.write("recover failed: %r\n" % e)
    threading.Thread(target=refresh_agents, daemon=True).start()
    last_hb = 0
    last_usage = 0
    last_upd = 0
    while not STOP.is_set():
        try:
            r = http("/api/daemon/poll?wait=20", timeout=40)
        except Exception as e:
            sys.stderr.write("poll failed: %r\n" % e)
            time.sleep(3)
            continue
        if r.get("job"):
            threading.Thread(target=run_job, args=(r["job"], r["agent"], r.get("files")),
                             daemon=True).start()
        if time.time() - last_hb > 20:
            heartbeat()
            last_hb = time.time()
        if time.time() - last_upd > 600:
            last_upd = time.time()
            threading.Thread(target=self_update, daemon=True).start()
        if time.time() - last_usage > 3600:
            threading.Thread(target=post_usage, daemon=True).start()
            last_usage = time.time()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "send":
        sys.exit(cli_send(sys.argv[2], sys.argv[3:]))
    main()

