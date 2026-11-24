#!/usr/bin/env python3
"""AI 中枢 · Daemon（每台机器一个）

向 Hub 长轮询领任务；为每个 agent 拉起一个 `kimi acp` 子进程；流式回传；本地记账；
心跳；产出扫描与上传。不保存任何模型密钥。
"""
import json, mimetypes, os, queue, shutil, subprocess, sys, threading, time, urllib.request

CONF = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), encoding="utf-8"))
HUB = CONF["hub"].rstrip("/")
TOKEN = CONF["token"]
NAME = CONF.get("name") or "未命名主机"
LOG_DIR = os.path.expanduser(CONF.get("log_dir", "~/aihub/logs"))
os.makedirs(LOG_DIR, exist_ok=True)

PROCS, LOCK, STOP = {}, threading.Lock(), threading.Event()
ACTIVE, KNOWN, WHERES = set(), set(), {}
KIMI_CONF = (CONF.get("kimi") or "").strip()


def kimi_argv():
    """Windows 上 kimi 可能是 .cmd/.bat，需要经 cmd /c 才能拉起。"""
    exe = KIMI_CONF or shutil.which("kimi") or "kimi"
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe]
    return [exe]


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


class Acp:
    def __init__(self, agent):
        self.agent, self.p, self.sid = agent, None, None
        self.seq, self.lock = 1, threading.Lock()
        self.wlock = threading.Lock()
        self.pending, self.notify = {}, queue.Queue()
        self.last_used = time.time()
        self.skills_sig = ""
        cwd = agent["cwd"]
        os.makedirs(cwd, exist_ok=True)

    def materialize_skills(self):
        sig = ",".join(sorted(x["id"] + ":" + str(x.get("version", "")) for x in (self.agent.get("skills") or [])))
        if sig != self.skills_sig and self.p:
            sys.stderr.write("技能变更，重启实例: %s\n" % self.agent["id"])
            self.kill()
        self.skills_sig = sig
        """把启用中的技能写到 <cwd>/skills/ 并在 AGENTS.md 里列出。"""
        cwd = self.agent["cwd"]
        skills = self.agent.get("skills") or []
        base = os.path.join(cwd, "skills")
        os.makedirs(base, exist_ok=True)
        for sk in skills:
            d = os.path.join(base, sk["id"])
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("# %s\n\n> %s\n\n**何时使用**：%s\n\n%s\n" % (
                    sk.get("name", ""), sk.get("summary", ""), sk.get("when_to_use", ""),
                    sk.get("body", "")))
        lines = ["# %s\n\n%s\n" % (self.agent.get("name", ""), self.agent.get("prompt", ""))]
        if skills:
            lines.append("\n## 可用技能\n\n需要时先读对应文件，再按里面的步骤做：\n")
            for sk in skills:
                lines.append("- **%s**：%s（何时用：%s） → `skills/%s/SKILL.md`\n" % (
                    sk.get("name", ""), sk.get("summary", ""), sk.get("when_to_use", ""), sk["id"]))
        with open(os.path.join(cwd, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("".join(lines))

    def ensure(self):
        self.materialize_skills()
        sys.stderr.write("[acp] ensure %s p=%r sid=%r\n" % (self.agent["id"], bool(self.p), self.sid)); sys.stderr.flush()
        if self.p and self.p.poll() is None and self.sid:
            return
        sys.stderr.write("[acp] spawn kimi acp\n"); sys.stderr.flush()
        self.p = subprocess.Popen(kimi_argv() + ["acp"], cwd=self.agent["cwd"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                  encoding="utf-8", errors="replace")
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

    def prompt_stream(self, parts, on_chunk, is_canceled):
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


def scan_artifacts(cwd, since):
    skip = {".git", "node_modules", "__pycache__", ".kimi", ".kimi-code", ".aihub-inbox"}
    skip_files = {"AGENTS.md", ".DS_Store"}
    out = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            p = os.path.join(root, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if fn in skip_files:
                continue
            if st.st_mtime >= since and 0 < st.st_size <= 40 * 1024 * 1024:
                out.append((p, st.st_size, st.st_mtime))
    out.sort(key=lambda x: -x[2])
    return out[:20]


def run_job(job, agent, files):
    jid, cwd, started = job["id"], agent["cwd"], time.time()
    sys.stderr.write("[job] start %s agent=%s cwd=%s\n" % (jid, agent["id"], cwd)); sys.stderr.flush()
    try:
        http("/api/daemon/event", {"job": jid, "kind": "start"}, timeout=15)
    except Exception as e:
        sys.stderr.write("start ack failed: %r\n" % e)
    os.makedirs(cwd, exist_ok=True)
    text = job["text"]
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
            if time.time() - state["last_text"] > 300:
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
    sys.stderr.write("[job] acp ready\n"); sys.stderr.flush()
    try:
        stop = a.prompt_stream(parts, on_chunk, lambda: state["cancel"])
        push(force=True)
        if stop == "canceled" or state["cancel"]:
            http("/api/daemon/event", {"job": jid, "kind": "error", "error": "已中止"}, timeout=30)
        else:
            http("/api/daemon/event", {"job": jid, "kind": "done"}, timeout=30)
    except Exception as e:
        sys.stderr.write("job failed: %r\n" % e)
        push(force=True)
        http("/api/daemon/event", {"job": jid, "kind": "error", "error": str(e)[:300]}, timeout=30)

    for p, size, _ in scan_artifacts(cwd, started):
        try:
            r = http("/api/daemon/artifact", {"agent": agent["id"], "name": os.path.basename(p),
                                              "size": size, "path": p, "job": jid,
                                              "mime": mimetypes.guess_type(p)[0] or ""}, timeout=30)
            if size <= 5 * 1024 * 1024:
                with open(p, "rb") as f:
                    http("/api/daemon/upload", f.read(),
                         headers={"X-File-Id": r["id"], "Content-Type": "application/octet-stream"},
                         raw=True, timeout=120)
        except Exception as e:
            sys.stderr.write("artifact failed: %r\n" % e)

    with LOCK:
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


def heartbeat():
    with LOCK:
        ids = set(KNOWN) | set(PROCS)
        agents = []
        for aid in ids:
            p = PROCS.get(aid)
            alive = bool(p and p.p and p.p.poll() is None)
            state = "busy" if aid in ACTIVE else ("on" if alive else "off")
            agents.append({"id": aid, "state": state})
    try:
        http("/api/daemon/hb", {"agents": agents, "host": NAME}, timeout=15)
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
            for op in d.get("ops", []):
                try:
                    a = PROCS.pop(op["agent"], None)
                    if a:
                        a.kill()
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
        time.sleep(60)


def main():
    print("daemon -> %s" % HUB, flush=True)
    try:
        http("/api/daemon/recover", {}, timeout=15)
        print("recovered stale jobs", flush=True)
    except Exception as e:
        sys.stderr.write("recover failed: %r\n" % e)
    threading.Thread(target=refresh_agents, daemon=True).start()
    last_hb = 0
    last_usage = 0
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
        if time.time() - last_usage > 3600:
            threading.Thread(target=post_usage, daemon=True).start()
            last_usage = time.time()


if __name__ == "__main__":
    main()
