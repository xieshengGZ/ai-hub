#!/usr/bin/env python3
"""Hub 主链路回归测试：发消息 -> 领任务 -> 流式回传 -> 完成。只用标准库。"""

import http.cookiejar
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(ROOT, "hub", "config.json")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_health(base, proc):
    for _ in range(80):
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise SystemExit("hub exited early:\n" + err[-3000:])
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=1) as r:
                if json.loads(r.read()).get("ok"):
                    return
        except Exception:
            pass
        time.sleep(0.25)
    raise SystemExit("hub did not become healthy")


def main():
    backup = open(CONF, "rb").read() if os.path.exists(CONF) else None
    tmp = tempfile.mkdtemp(prefix="aihub-flow-")
    data = os.path.join(tmp, "data")
    os.makedirs(data, exist_ok=True)
    port = free_port()
    with open(CONF, "w", encoding="utf-8") as f:
        json.dump({"password": "ci", "data": data, "port": port}, f)

    proc = subprocess.Popen(
        [sys.executable, "hub/hub.py"], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        base = "http://127.0.0.1:%d" % port
        wait_health(base, proc)

        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

        def post(path, obj, headers=None):
            req = urllib.request.Request(
                base + path, data=json.dumps(obj).encode(),
                headers={"Content-Type": "application/json", **(headers or {})},
                method="POST")
            return json.loads(opener.open(req, timeout=10).read())

        def get(path, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            return json.loads(opener.open(req, timeout=10).read())

        assert post("/api/login", {"password": "ci"}).get("ok") is True

        db = os.path.join(data, "aihub.db")
        con = sqlite3.connect(db)
        con.execute("INSERT INTO hosts(name,token,created,last_seen) VALUES('box','tok',0,0)")
        con.execute(
            "INSERT INTO agents(id,name,host,monogram,cwd,model,prompt,sort,always_on,deleted) "
            "VALUES('t1','Test agent','box','T',?,'','',1,0,0)", (tmp,))
        con.commit()
        con.close()

        sent = post("/api/send", {"agent": "t1", "text": "hello", "mode": "full"})
        jid = sent["job"]

        poll = get("/api/daemon/poll?wait=2", {"X-Daemon": "tok"})
        assert poll.get("job") and poll["job"]["id"] == jid, poll

        daemon_hdr = {"X-Daemon": "tok"}
        assert post("/api/daemon/event", {"job": jid, "kind": "start"}, daemon_hdr)["cancel"] is False
        post("/api/daemon/event", {"job": jid, "kind": "chunk", "text": "你好，"}, daemon_hdr)
        post("/api/daemon/event", {"job": jid, "kind": "chunk", "text": "流式回复。"}, daemon_hdr)
        post("/api/daemon/event", {"job": jid, "kind": "done"}, daemon_hdr)

        d = get("/api/msg?agent=t1")
        assert d["job"]["status"] == "done", d["job"]
        msgs = d["messages"]
        assert [m["role"] for m in msgs] == ["me", "agent"], msgs
        assert msgs[0]["text"] == "hello", msgs[0]
        assert msgs[1]["text"] == "你好，流式回复。", msgs[1]
        assert msgs[1]["status"] == "done", msgs[1]
        print("hub flow test: ok")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
        if backup is None:
            try:
                os.remove(CONF)
            except OSError:
                pass
        else:
            with open(CONF, "wb") as f:
                f.write(backup)


if __name__ == "__main__":
    main()
