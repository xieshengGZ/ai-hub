#!/usr/bin/env python3
"""AI 中枢 · Hub（单进程，仅标准库）

职责：会话与消息存储、Agent 名册、任务派发排队、文件收发、给 Daemon 的长轮询接口。
不负责：模型调用（在 Daemon 侧）、渲染（在浏览器）。
"""
import base64, hashlib, hmac, json, mimetypes, os, re, sqlite3, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
CONF = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
DATA = CONF["data"]
DB = os.path.join(DATA, "aihub.db")
FILES = os.path.join(DATA, "files")
INBOX = os.path.join(DATA, "inbox")           # 附件暂存：inbox/<agent>/<YYYYMMDD>/
UPLOAD_LOG = os.path.join(INBOX, "uploads.jsonl")
MON = os.path.join(DATA, "monitor")
MON_TOKEN = CONF.get("monitor_token") or ""
os.makedirs(MON, exist_ok=True)
STATIC = os.path.join(ROOT, "static")
_here = os.path.dirname(os.path.abspath(__file__))
DAEMON_SRC = os.path.join(os.path.dirname(_here), "daemon", "daemon.py")
if not os.path.exists(DAEMON_SRC):
    DAEMON_SRC = os.path.join(_here, "daemon.py")
os.makedirs(FILES, exist_ok=True)
os.makedirs(INBOX, exist_ok=True)

DB_LOCK = threading.RLock()
WAIT = threading.Condition()          # 有新任务时唤醒长轮询
REV = [0]                             # 全局版本号，任何写操作都 +1
REV_COND = threading.Condition()


def bump():
    with REV_COND:
        REV[0] += 1
        REV_COND.notify_all()

PUSH_SH = r"""#!/usr/bin/env bash
# 监控推送：把当天 JSON 推到中枢。只用出站 HTTPS，不在本机开任何端口。
# 安装：curl -fsSL "<中枢>/hub/api/monitor/push.sh?t=<token>" | bash -s -- <node> install
set -euo pipefail
NODE="${1:?node}"
MODE="${2:-push}"
DAY="${3:-$(TZ=Asia/Shanghai date +%F)}"
HUB="${HUB:-https://nykj.store/hub}"
HUB_IP="${HUB_IP:-39.108.175.43}"     # 免 DNS：直接按 IP 连，证书仍按域名校验
TOK="__TOKEN__"
DIR="${MONITOR_DIR:-$HOME/monitor}"
SELF="$HOME/aihub-monitor/push.sh"

if [ "$MODE" = "install" ]; then
  mkdir -p "$(dirname "$SELF")"
  curl -fsSL --resolve "nykj.store:443:$HUB_IP" "$HUB/api/monitor/push.sh?t=$TOK" -o "$SELF"
  chmod +x "$SELF"
  ( crontab -l 2>/dev/null | grep -v 'aihub-monitor/push.sh' || true
    echo "30 6 * * * MONITOR_DIR=$DIR $SELF $NODE push >> $HOME/aihub-monitor/push.log 2>&1"
    echo "@reboot sleep 120 && MONITOR_DIR=$DIR $SELF $NODE push >> $HOME/aihub-monitor/push.log 2>&1" ) | crontab -
  echo "已安装：$SELF"
  echo "计划：每天 06:30 推当天，开机 2 分钟后补一次"
  MODE=push
fi

cd "$DIR" 2>/dev/null || { echo "没有目录 $DIR"; exit 1; }
EXPORT=""
for c in "$DIR/export_daily.sh" "$DIR/export/export_daily.sh"; do
  [ -x "$c" ] && EXPORT="$c" && break
done
[ -n "$EXPORT" ] || { echo "找不到 export_daily.sh（$DIR 或 $DIR/export）"; exit 1; }
"$EXPORT" "$DAY" >/dev/null || { echo "导出失败"; exit 1; }
f=""
for cand in "$DIR/export/${NODE}-${DAY}.json" "$DIR/export/${NODE}-$(echo "$DAY" | tr -d -).json"; do
  [ -f "$cand" ] && f="$cand" && break
done
[ -n "$f" ] || { echo "没找到导出文件（export/${NODE}-${DAY}.json）"; exit 1; }
gzip -9c "$f" | curl -fsS --resolve "nykj.store:443:$HUB_IP" -X POST "$HUB/api/monitor/ingest" \
  -H "X-Monitor-Token: $TOK" -H "X-Node: $NODE" -H "X-Date: $DAY" \
  -H "Content-Type: application/gzip" --data-binary @-
echo
"""


SEED = [
    {
        "id": "main", "name": "主 agent", "host": "中枢机", "monogram": "主", "sort": 1,
        "cwd": "/home/ubuntu/aihub/agents/main",
        "prompt": "你是这台机器上的调度助理，用户的默认入口。回答简洁、先给结论。"
                  "涉及别的机器或长任务时，先说清楚你准备怎么做，再动手。不知道就直说，不要编。",
    },
    {
        "id": "ops", "name": "值班", "host": "中枢机", "monogram": "值", "sort": 2,
        "cwd": "/home/ubuntu/aihub/agents/ops",
        "prompt": "你负责值班运维：看日志、查服务状态、排查异常。回答要带证据（命令输出、时间点），"
                  "不要凭印象下结论。",
    },
    {
        "id": "write", "name": "写作", "host": "中枢机", "monogram": "写", "sort": 3,
        "cwd": "/home/ubuntu/aihub/agents/write",
        "prompt": "你负责中文文档写作与润色：先问清对象与目的，再出稿；用书面语，避免空话。",
    },
]


def db():
    conn = sqlite3.connect(DB, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init():
    with DB_LOCK, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS agents(
          id TEXT PRIMARY KEY, name TEXT, host TEXT, monogram TEXT, cwd TEXT,
          model TEXT, prompt TEXT, sort INTEGER DEFAULT 9, always_on INTEGER DEFAULT 0,
          managed_by TEXT, deleted INTEGER DEFAULT 0,
          status TEXT DEFAULT 'off', seen REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, role TEXT, text TEXT,
          ts REAL, status TEXT DEFAULT 'done', job TEXT, mode TEXT DEFAULT 'full');
        CREATE TABLE IF NOT EXISTS jobs(
          claimed REAL,
          id TEXT PRIMARY KEY, agent TEXT, user_msg INTEGER, agent_msg INTEGER,
          status TEXT DEFAULT 'queued', started REAL, ended REAL, error TEXT,
          text TEXT DEFAULT '', daemon TEXT, origin TEXT);
        CREATE TABLE IF NOT EXISTS files(
          id TEXT PRIMARY KEY, name TEXT, size INTEGER, mime TEXT, agent TEXT,
          kind TEXT, path TEXT, ts REAL, msg INTEGER, stored TEXT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS models(
          id TEXT PRIMARY KEY, name TEXT, base_url TEXT, model_id TEXT, api_key TEXT,
          kind TEXT DEFAULT 'openai', max_context INTEGER DEFAULT 131072, created REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS skills(
          id TEXT, name TEXT, summary TEXT, when_to_use TEXT, body TEXT,
          version TEXT DEFAULT '1', created REAL DEFAULT 0,
          source TEXT DEFAULT 'hub', origin_host TEXT DEFAULT '',
          UNIQUE(id, origin_host));
        CREATE TABLE IF NOT EXISTS agent_skills(
          agent TEXT, skill TEXT, enabled INTEGER DEFAULT 1, PRIMARY KEY(agent, skill));
        CREATE TABLE IF NOT EXISTS hosts(
          name TEXT PRIMARY KEY, token TEXT, created REAL DEFAULT 0, last_seen REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS daemon_ops(
          id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, op TEXT, path TEXT,
          done INTEGER DEFAULT 0, ts REAL);
        CREATE TABLE IF NOT EXISTS usage_daily(
          agent TEXT, day TEXT, jobs INTEGER DEFAULT 0, ms INTEGER DEFAULT 0,
          PRIMARY KEY(agent, day));
        CREATE INDEX IF NOT EXISTS ix_msg_agent ON messages(agent, id);
        CREATE INDEX IF NOT EXISTS ix_job_status ON jobs(status);
        """)
        for col in ("managed_by TEXT", "deleted INTEGER DEFAULT 0"):
            try:
                c.execute("ALTER TABLE agents ADD COLUMN %s" % col)
            except Exception:
                pass
        try:
            c.execute("ALTER TABLE models ADD COLUMN max_context INTEGER DEFAULT 131072")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE daemon_ops ADD COLUMN host TEXT")
        except Exception:
            pass
        cols = [r[1] for r in c.execute("PRAGMA table_info(skills)")]
        ddl = (c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='skills'").fetchone() or [""])[0] or ""
        need_rebuild = ("origin_host" not in cols) or ("id TEXT PRIMARY KEY" in ddl)
        if need_rebuild:
            c.executescript("""
              DROP TABLE IF EXISTS skills_new;
              CREATE TABLE skills_new(
                id TEXT, name TEXT, summary TEXT, when_to_use TEXT, body TEXT,
                version TEXT DEFAULT '1', created REAL DEFAULT 0,
                source TEXT DEFAULT 'hub', origin_host TEXT DEFAULT '',
                UNIQUE(id, origin_host));
              INSERT OR IGNORE INTO skills_new(id,name,summary,when_to_use,body,version,created,source,origin_host)
                SELECT id,name,summary,when_to_use,body,version,created,
                       COALESCE(source,'hub'), COALESCE(origin_host,'') FROM skills;
              DROP TABLE skills;
              ALTER TABLE skills_new RENAME TO skills;
            """)
        for col in ("source TEXT DEFAULT 'hub'", "origin_host TEXT"):
            try:
                c.execute("ALTER TABLE skills ADD COLUMN %s" % col)
            except Exception:
                pass
        for col in ("token TEXT", "created REAL DEFAULT 0"):
            try:
                c.execute("ALTER TABLE hosts ADD COLUMN %s" % col)
            except Exception:
                pass
        for col in ("managed_by TEXT", "deleted INTEGER DEFAULT 0"):
            try:
                c.execute("ALTER TABLE agents ADD COLUMN %s" % col)
            except Exception:
                pass
        for col in ("origin",):
            try:
                c.execute("ALTER TABLE jobs ADD COLUMN %s TEXT" % col)
            except Exception:
                pass
        for col in ("always_on",):
            try:
                c.execute("ALTER TABLE agents ADD COLUMN %s INTEGER DEFAULT 0" % col)
            except Exception:
                pass
        for col in ("claimed",):
            try:
                c.execute("ALTER TABLE jobs ADD COLUMN %s REAL" % col)
            except Exception:
                pass
        # 主机由 Daemon 心跳注册；这里不再预置"中枢机"（Hub 自己不跑 Daemon）
        try:
            c.execute("UPDATE agents SET managed_by='main' WHERE id IN ('ops','write','doc') AND managed_by IS NULL")
        except Exception:
            pass


def cookie_value():
    return hmac.new(CONF["secret"].encode(), b"aihub-session", hashlib.sha256).hexdigest()


def daemon_ok(handler):
    got = handler.headers.get("X-Daemon") or ""
    if not got:
        return False
    if CONF.get("token") and hmac.compare_digest(got, CONF["token"]):
        return True
    try:
        for h in rows("SELECT token FROM hosts WHERE token IS NOT NULL"):
            if h["token"] and hmac.compare_digest(got, h["token"]):
                return True
    except Exception:
        pass
    return False


def host_by_token(handler_or_tok):
    tok = handler_or_tok.headers.get("X-Daemon") if hasattr(handler_or_tok, "headers") else handler_or_tok
    if not tok:
        return None
    if CONF.get("token") and hmac.compare_digest(tok, CONF["token"]):
        return CONF.get("legacy_host", "腾讯云-Daemon")
    try:
        for h in rows("SELECT name, token FROM hosts WHERE token IS NOT NULL"):
            if h["token"] and hmac.compare_digest(tok, h["token"]):
                return h["name"]
    except Exception:
        pass
    return None
    for h in rows("SELECT name,token FROM hosts WHERE token IS NOT NULL"):
        if h["token"] and hmac.compare_digest(tok, h["token"]):
            return h["name"]
    return None


def check_cookie(handler):
    raw = handler.headers.get("Cookie", "")
    m = re.search(r"sid=([0-9a-f]{64})", raw)
    return bool(m) and hmac.compare_digest(m.group(1), cookie_value())


def now():
    return time.time()


def rows(sql, args=()):
    with DB_LOCK, db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    with DB_LOCK, db() as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


class Api(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


# ---------------- 业务 ----------------

def bootstrap():
    ags = rows("""SELECT id,name,host,monogram,status,seen,always_on,managed_by,cwd,model,
                         (SELECT COUNT(*) FROM agents x WHERE x.managed_by=agents.id AND x.deleted=0) AS team
                  FROM agents WHERE deleted=0 ORDER BY sort,id""")
    out = []
    for a in ags:
        last = one("""SELECT text, ts, role FROM messages WHERE agent=? ORDER BY id DESC LIMIT 1""", (a["id"],))
        busy = one("SELECT id FROM jobs WHERE agent=? AND status IN('queued','running') LIMIT 1", (a["id"],))
        unread = one("SELECT COUNT(*) n FROM messages WHERE agent=? AND role='agent' AND status='done' AND ts>?",
                     (a["id"], a["seen"] or 0))["n"]
        out.append({
            "id": a["id"], "name": a["name"], "host": a["host"], "monogram": a["monogram"],
            "state": "busy" if busy else ("on" if a["status"] in ("on", "busy") else "off"),
            "last": (last or {}).get("text", "")[:80].replace("\n", " "),
            "ts": (last or {}).get("ts", 0), "unread": unread,
            "always_on": bool(a.get("always_on")),
            "managed_by": a.get("managed_by"),
            "cwd": a.get("cwd"),
            "model": a.get("model") or "",
            "team": a.get("team") or 0,
        })
    return {"agents": out, "now": now()}


def session_get(agent, after=0, before=0, limit=0):
    ag = one("SELECT * FROM agents WHERE id=? AND deleted=0", (agent,))
    if not ag:
        raise Api(404, "没有这个 agent")
    lim = max(1, min(int(limit or 50), 400))
    has_more = False
    if after:
        msgs = rows("""SELECT id,role,text,ts,status,job,mode FROM messages
                       WHERE agent=? AND id>? ORDER BY id LIMIT 400""", (agent, after))
    elif before:
        msgs = rows("""SELECT id,role,text,ts,status,job,mode FROM messages
                       WHERE agent=? AND id<? ORDER BY id DESC LIMIT ?""", (agent, before, lim))
        msgs.reverse()
        if msgs:
            has_more = bool(one("SELECT 1 FROM messages WHERE agent=? AND id<? LIMIT 1",
                                (agent, msgs[0]["id"])))
    else:
        msgs = rows("""SELECT id,role,text,ts,status,job,mode FROM messages
                       WHERE agent=? ORDER BY id DESC LIMIT ?""", (agent, lim))
        msgs.reverse()
        if msgs:
            has_more = bool(one("SELECT 1 FROM messages WHERE agent=? AND id<? LIMIT 1",
                                (agent, msgs[0]["id"])))
    if msgs:
        ids = [m["id"] for m in msgs]
        ph = ",".join("?" * len(ids))
        fmap = {}
        for f in rows("""SELECT id,name,size,mime,kind AS dir,msg FROM files
                         WHERE agent=? AND msg IN (%s) ORDER BY ts""" % ph, [agent] + ids):
            f["kind"] = file_kind(f["mime"], f["name"])
            f["url"] = "api/file?id=" + f["id"]
            fmap.setdefault(f["msg"], []).append(f)
        for m in msgs:
            m["files"] = fmap.get(m["id"], [])
    else:
        for m in msgs:
            m["files"] = []
    job = one("""SELECT id,status,error,started,ended,agent_msg FROM jobs
                 WHERE agent=? ORDER BY started DESC LIMIT 1""", (agent,))
    run("UPDATE agents SET seen=? WHERE id=?", (now(), agent))
    busy_ = one("SELECT id FROM jobs WHERE agent=? AND status IN('queued','running')", (agent,))
    state = "busy" if busy_ else ("on" if ag["status"] in ("on", "busy") else "off")
    return {"agent": {"id": ag["id"], "name": ag["name"], "host": ag["host"],
                      "monogram": ag["monogram"], "status": ag["status"], "state": state,
                      "always_on": bool(ag["always_on"]),
                      "mode": (one("SELECT mode FROM messages WHERE agent=? ORDER BY id DESC LIMIT 1",
                                   (agent,)) or {}).get("mode", "full")},
            "messages": msgs, "job": job, "has_more": has_more}


def session_send(agent, text, mode="full"):
    text = (text or "").strip()
    if not text:
        raise Api(400, "空消息")
    ag = one("SELECT id FROM agents WHERE id=? AND deleted=0", (agent,))
    if not ag:
        raise Api(410, "这个会话已删除或不存在，回会话列表重新打开再发")
    if one("SELECT id FROM jobs WHERE agent=? AND status IN('queued','running')", (agent,)):
        raise Api(409, "这个 agent 正在忙，等它跑完再发")
    uid = run("INSERT INTO messages(agent,role,text,ts,status,mode) VALUES(?,?,?,?,?,?)",
              (agent, "me", text, now(), "done", mode))
    aid = run("INSERT INTO messages(agent,role,text,ts,status,mode) VALUES(?,?,?,?,?,?)",
              (agent, "agent", "", now(), "running", mode))
    jid = uuid.uuid4().hex[:12]
    run("INSERT INTO jobs(id,agent,user_msg,agent_msg,status) VALUES(?,?,?,?,'queued')",
        (jid, agent, uid, aid))
    with WAIT:
        WAIT.notify_all()
    bump()
    return {"user_msg": uid, "agent_msg": aid, "job": jid}


def sweep_stale():
    rows_ = rows("SELECT id, agent_msg FROM jobs WHERE status='running' AND started IS NOT NULL AND started < ?",
                 (now() - 1200,))
    for j in rows_:
        run("UPDATE jobs SET status='error', ended=?, error='超时，守护进程可能掉线' WHERE id=?", (now(), j["id"]))
        run("UPDATE messages SET status='error', text=CASE WHEN text='' THEN '任务超时：守护进程掉线或没有响应' ELSE text END WHERE id=?",
            (j["agent_msg"],))
    return len(rows_)


def job_cancel(agent):
    j = one("SELECT id FROM jobs WHERE agent=? AND status IN('queued','running')", (agent,))
    if not j:
        return {"ok": False}
    run("UPDATE jobs SET status='canceled', ended=? WHERE id=?", (now(), j["id"]))
    run("UPDATE messages SET status='canceled' WHERE id=(SELECT agent_msg FROM jobs WHERE id=?)", (j["id"],))
    bump()
    return {"ok": True}


def daemon_poll(wait_s, host=""):
    end = time.time() + min(wait_s, 28)
    while True:
        with DB_LOCK:
            j = one("""SELECT j.* FROM jobs j JOIN agents a ON a.id=j.agent
                       WHERE j.status='queued' AND (j.claimed IS NULL OR j.claimed < ?)
                         AND a.host=? ORDER BY j.rowid LIMIT 1""", (now() - 30, host))
            if j:
                ag = one("SELECT * FROM agents WHERE id=?", (j["agent"],))
                run("UPDATE jobs SET claimed=? WHERE id=?", (now(), j["id"]))
                run("UPDATE agents SET status='on', seen=? WHERE id=?", (now(), j["agent"]))
                txt = one("SELECT text FROM messages WHERE id=?", (j["user_msg"],))["text"] or ""
                pool = rows("""SELECT id,name,mime,path,msg FROM files
                               WHERE kind='in' AND agent=? AND stored IS NOT NULL
                               ORDER BY ts DESC LIMIT 20""", (j["agent"],))
                picked = [f for f in pool if os.path.splitext(f["name"])[0] in txt or f["name"] in txt]
                if len(picked) < 8:
                    for f in pool:
                        if len(picked) >= 8:
                            break
                        if not f["msg"] and f not in picked:
                            picked.append(f)
                for f in picked:
                    run("UPDATE files SET msg=? WHERE id=?", (j["user_msg"], f["id"]))
                files = picked
                skills = rows("""SELECT s.id, s.name, s.summary, s.when_to_use, s.body, s.version
                                   FROM skills s JOIN agent_skills a ON a.skill=s.id
                                   WHERE a.agent=? AND a.enabled=1""", (ag["id"],))
                return {"job": {"id": j["id"], "text": one("SELECT text FROM messages WHERE id=?",
                                                           (j["user_msg"],))["text"]},
                        "agent": {"id": ag["id"], "name": ag["name"], "cwd": ag["cwd"],
                                  "prompt": ag["prompt"], "model": ag["model"],
                                  "model_cfg": model_cfg_for(ag["model"]), "skills": skills},
                        "files": [{"id": f["id"], "name": f["name"],
                                   "mime": f["mime"] or "",
                                   "kind": file_kind(f["mime"], f["name"])} for f in files]}
        if time.time() >= end:
            return {"job": None}
        with WAIT:
            WAIT.wait(timeout=1.0)


def daemon_event(d):
    jid = d.get("job")
    j = one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not j:
        raise Api(404, "没有这个任务")
    if j["status"] == "canceled":
        return {"cancel": True}
    if d.get("kind") == "start":
        run("UPDATE jobs SET status='running', started=? WHERE id=? AND status='queued'", (now(), jid))
        return {"cancel": False}
    if d.get("kind") == "chunk":
        txt = d.get("text") or ""
        if txt:
            run("UPDATE jobs SET text=text||? WHERE id=?", (txt, jid))
            run("UPDATE messages SET text=text||? WHERE id=?", (txt, j["agent_msg"]))
    elif d.get("kind") == "done":
        txt = d.get("text") or ""
        if txt:
            run("UPDATE messages SET text=? WHERE id=? AND text=''", (txt, j["agent_msg"]))
        run("UPDATE jobs SET status='done', ended=? WHERE id=?", (now(), jid))
        run("UPDATE messages SET status='done' WHERE id=?", (j["agent_msg"],))
        bump()
    elif d.get("kind") == "error":
        run("UPDATE jobs SET status='error', ended=?, error=? WHERE id=?", (now(), d.get("error", ""), jid))
        bump()
        run("UPDATE messages SET status='error', text=CASE WHEN text='' THEN ? ELSE text END WHERE id=?",
            (d.get("error", "执行失败"), j["agent_msg"]))
    return {"cancel": False}


def model_cfg_for(mid):
    if not mid:
        return None
    m = one("SELECT * FROM models WHERE id=?", (mid,))
    if not m:
        return None
    return {"id": m["id"], "name": m["name"], "base_url": m["base_url"],
            "model_id": m["model_id"], "api_key": m["api_key"],
            "kind": (m["kind"] or "openai"), "max_context": int(m["max_context"] or 131072)}


def daemon_hb(d):
    if d.get("host"):
        run("INSERT INTO hosts(name,last_seen) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
            (d["host"], now()))
    for a in d.get("agents", []):
        run("UPDATE agents SET status=?, seen=? WHERE id=?", (a.get("state", "on"), now(), a.get("id")))
    changed = 0
    host_ = d.get("host") or ""
    # 共享技能库（每台机器 ~/aihub/skills）+ 各 agent 的安装情况
    store = d.get("skill_store")
    if store is not None:
        keep = set()
        for sk in store:
            sid = re.sub(r"[^\w.-]", "-", (sk.get("id") or "").strip(), flags=re.U).strip("-")[:48]
            if not sid:
                continue
            keep.add(sid)
            run("""INSERT INTO skills(id,name,summary,when_to_use,body,version,created,source,origin_host)
                   VALUES(?,?,?,?,?,'1',?,'local',?)
                   ON CONFLICT(id, origin_host) DO UPDATE SET name=excluded.name,
                     summary=excluded.summary, when_to_use=excluded.when_to_use, source='local'""",
                (sid, (sk.get("name") or sid)[:80], (sk.get("summary") or "")[:300],
                 (sk.get("when_to_use") or "")[:300], "", now(), host_))
            changed += 1
        for r in rows("SELECT id FROM skills WHERE source='local' AND origin_host=?", (host_,)):
            if r["id"] not in keep:
                run("DELETE FROM skills WHERE id=? AND origin_host=?", (r["id"], host_))
                run("DELETE FROM agent_skills WHERE skill=? AND agent IN "
                    "(SELECT id FROM agents WHERE host=?)", (r["id"], host_))
                changed += 1
    inst = d.get("skill_installs")
    if inst is not None:
        host_skills = [r["id"] for r in rows("SELECT id FROM skills WHERE origin_host=?", (host_,))]
        for aid in [r["id"] for r in rows("SELECT id FROM agents WHERE host=? AND deleted=0", (host_,))]:
            want = set(inst.get(aid) or []) & set(host_skills)
            have = set(r["skill"] for r in rows("SELECT skill FROM agent_skills WHERE agent=?", (aid,))) & set(host_skills)
            for s2 in want - have:
                run("INSERT INTO agent_skills(agent,skill,enabled) VALUES(?,?,1) "
                    "ON CONFLICT(agent,skill) DO UPDATE SET enabled=1", (aid, s2))
                changed += 1
            for s2 in have - want:
                run("DELETE FROM agent_skills WHERE agent=? AND skill=?", (aid, s2))
                changed += 1
    if changed:
        bump()
    return {"ok": True}


def artifact_add(d):
    fid = uuid.uuid4().hex[:12]
    msg = d.get("msg")
    if msg is None and d.get("job"):
        j = one("SELECT agent_msg FROM jobs WHERE id=?", (d["job"],))
        msg = (j or {}).get("agent_msg")
    if d.get("latest"):
        m = one("SELECT id FROM messages WHERE agent=? AND role='agent' ORDER BY id DESC LIMIT 1",
                (d.get("agent") or "",))
        if m:
            msg = m["id"]
    run("""INSERT INTO files(id,name,size,mime,agent,kind,path,ts,msg,stored)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (fid, d.get("name", "file"), int(d.get("size") or 0), d.get("mime") or "",
         d.get("agent"), "out", d.get("path", ""), now(), msg, d.get("stored")))
    return {"id": fid}


def artifacts_list(agent):
    return {"files": rows("""SELECT id,name,size,ts,path,stored FROM files
                             WHERE kind='out' AND agent=? ORDER BY ts DESC LIMIT 50""", (agent,))}


def file_kind(mime, name):
    m = (mime or "").lower()
    n = (name or "").lower()
    if m.startswith("image/") or re.search(r"\.(png|jpe?g|gif|webp|heic|heif|bmp)$", n):
        return "image"
    if m.startswith("video/") or re.search(r"\.(mp4|mov|m4v|webm|avi|mkv)$", n):
        return "video"
    return "file"


def upload(handler, agent):
    if not one("SELECT id FROM agents WHERE id=? AND deleted=0", (agent,)):
        raise Api(410, "这个会话已删除或不存在，回会话列表重新打开再传")
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0 or n > 120 * 1024 * 1024:
        raise Api(400, "文件为空或超过 120MB")
    raw = handler.headers.get("X-Filename") or "file"
    name = __import__("urllib.parse").parse.unquote(raw)
    try:
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    name = re.sub(r"[\\/\x00-\x1f]", "_", name)[:120]
    fid = uuid.uuid4().hex[:12]
    day = time.strftime("%Y%m%d")
    folder = os.path.join(INBOX, agent, day)
    os.makedirs(folder, exist_ok=True)
    stored = os.path.join(folder, "%s_%s" % (fid, name))
    with open(stored, "wb") as f:
        left = n
        while left > 0:
            b = handler.rfile.read(min(left, 1 << 20))
            if not b:
                break
            f.write(b)
            left -= len(b)
    size = os.path.getsize(stored)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    run("""INSERT INTO files(id,name,size,mime,agent,kind,path,ts,stored)
           VALUES(?,?,?,?,?,'in',?,?,?)""",
        (fid, name, size, mime, agent, "", now(), stored))
    rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": agent, "id": fid,
           "name": name, "size": size, "mime": mime, "path": stored}
    with DB_LOCK:
        with open(UPLOAD_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"id": fid, "name": name, "size": size, "kind": file_kind(mime, name)}


class Handler(BaseHTTPRequestHandler):
    server_version = "aihub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- 基础 ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            raise Api(400, "请求体不是 JSON")

    def _authed(self):
        if check_cookie(self) or daemon_ok(self):
            return True
        self._json({"error": "未登录"}, 401)
        return False

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p.startswith("/hub/"):
            return self._static(p[4:])
        if not p.startswith("/api/"):
            return self._static(p.lstrip("/") or "index.html")
        if p in ("/", "/hub", "/index.html"):
            return self._static("index.html")
        try:
            if p == "/api/bootstrap":
                if not self._authed():
                    return
                return self._json(bootstrap())
            if p == "/api/msg":
                if not self._authed():
                    return
                return self._json(session_get(
                    q.get("agent", [""])[0],
                    int(q.get("after", ["0"])[0] or 0),
                    int(q.get("before", ["0"])[0] or 0),
                    int(q.get("limit", ["0"])[0] or 0)))
            if p == "/api/host/create":
                d = self._body()
                name = (d.get("name") or "").strip()
                if not name:
                    raise Api(400, "要填主机名")
                if one("SELECT name FROM hosts WHERE name=?", (name,)):
                    raise Api(409, "这台主机已经有了")
                tok = uuid.uuid4().hex + uuid.uuid4().hex[:8]
                run("INSERT INTO hosts(name,token,created,last_seen) VALUES(?,?,?,0)",
                    (name, tok, now()))
                return self._json({"ok": True, "name": name, "token": tok})
            if p == "/api/host/token":
                d = self._body()
                h = one("SELECT name, token FROM hosts WHERE name=?", (d.get("name", ""),))
                if not h or not h["token"]:
                    raise Api(404, "没有这台主机")
                return self._json({"name": h["name"], "token": h["token"]})
            if p == "/api/host/rotate":
                d = self._body()
                name = (d.get("name") or "").strip()
                if not one("SELECT name FROM hosts WHERE name=?", (name,)):
                    raise Api(404, "没有这台主机")
                tok = uuid.uuid4().hex + uuid.uuid4().hex[:8]
                run("UPDATE hosts SET token=? WHERE name=?", (tok, name))
                return self._json({"ok": True, "token": tok})
            if p == "/api/host/delete":
                d = self._body()
                name = (d.get("name") or "").strip()
                n = one("SELECT COUNT(*) n FROM agents WHERE host=? AND deleted=0", (name,))["n"]
                if n:
                    raise Api(409, "这台主机上还有 %d 个 agent" % n)
                run("DELETE FROM hosts WHERE name=?", (name,))
                return self._json({"ok": True})
            if p == "/api/events":
                if not self._authed():
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last = -1
                try:
                    while True:
                        with REV_COND:
                            if REV[0] == last:
                                REV_COND.wait(timeout=15)
                        cur = REV[0]
                        self.wfile.write(("data: %d\n\n" % cur).encode())
                        self.wfile.flush()
                        last = cur
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            if p == "/api/hosts":
                if not self._authed():
                    return
                hs = rows("SELECT name, last_seen, created, (token IS NOT NULL) AS has_token FROM hosts ORDER BY name")
                for h in hs:
                    h["online"] = bool(h["last_seen"] and now() - h["last_seen"] < 90)
                    h["agents"] = one("SELECT COUNT(*) n FROM agents WHERE host=? AND deleted=0",
                                      (h["name"],))["n"]
                return self._json({"hosts": hs})
            if p == "/api/agents/archived":
                if not self._authed():
                    return
                return self._json({"agents": rows(
                    "SELECT id,name,host,monogram FROM agents WHERE deleted=1 ORDER BY sort,id")})
            if p == "/api/search":
                if not self._authed():
                    return
                q = (q.get("q", [""])[0] or "").strip()
                if len(q) < 1:
                    return self._json({"hits": []})
                like = "%" + q + "%"
                hits = rows("""SELECT m.id, m.agent, a.name as agent_name, m.role, m.ts,
                                      substr(replace(m.text, char(10), ' '), 1, 90) as snip
                               FROM messages m LEFT JOIN agents a ON a.id=m.agent
                               WHERE m.text LIKE ? AND m.text != ''
                               ORDER BY m.id DESC LIMIT 30""", (like,))
                files = rows("""SELECT id, name, agent, ts FROM files WHERE name LIKE ?
                                ORDER BY ts DESC LIMIT 10""", (like,))
                return self._json({"hits": hits, "files": files})
            if p == "/api/task/retry":
                d = self._body()
                j = one("SELECT * FROM jobs WHERE id=?", (d.get("job", ""),))
                if not j:
                    raise Api(404, "没有这个任务")
                if one("SELECT id FROM jobs WHERE agent=? AND status IN('queued','running')", (j["agent"],)):
                    raise Api(409, "目标 agent 正在忙")
                uid = run("INSERT INTO messages(agent,role,text,ts,status) VALUES(?,?,?,?,'done')",
                          (j["agent"], "me", "[重跑] " + (one("SELECT text FROM messages WHERE id=?",
                                                         (j["user_msg"],)) or {}).get("text", ""), now()))
                aid = run("INSERT INTO messages(agent,role,text,ts,status) VALUES(?,?,?,?,'running')",
                          (j["agent"], "agent", "", now()))
                jid = uuid.uuid4().hex[:12]
                run("INSERT INTO jobs(id,agent,user_msg,agent_msg,status,origin) VALUES(?,?,?,?,'queued',?)",
                    (jid, j["agent"], uid, aid, j.get("origin") or "重跑"))
                with WAIT:
                    WAIT.notify_all()
                return self._json({"job": jid})
            if p == "/api/tasks":
                if not self._authed():
                    return
                ag = q.get("agent", [""])[0]
                if ag:
                    rows_ = rows("""SELECT j.id, j.agent, a.name as agent_name, j.status, j.error,
                                           j.started, j.ended, j.origin, substr(m.text,1,120) as reply
                                    FROM jobs j LEFT JOIN agents a ON a.id=j.agent
                                    LEFT JOIN messages m ON m.id=j.agent_msg
                                    WHERE j.agent=? ORDER BY j.rowid DESC LIMIT 40""", (ag,))
                else:
                    rows_ = rows("""SELECT j.id, j.agent, a.name as agent_name, j.status, j.error,
                                           j.started, j.ended, j.origin, substr(m.text,1,120) as reply
                                    FROM jobs j LEFT JOIN agents a ON a.id=j.agent
                                    LEFT JOIN messages m ON m.id=j.agent_msg
                                    ORDER BY j.rowid DESC LIMIT 40""")
                return self._json({"tasks": rows_})
            if p == "/api/usage":
                if not self._authed():
                    return
                return self._json({"usage": rows(
                    "SELECT agent, day, jobs, ms FROM usage_daily ORDER BY day DESC, agent LIMIT 60")})
            if p == "/api/artifacts":
                if not self._authed():
                    return
                return self._json(artifacts_list(q.get("agent", [""])[0]))
            if p == "/api/monitor/list":
                if not self._authed():
                    return
                out = []
                for node in os.listdir(MON):
                    for f in os.listdir(os.path.join(MON, node)):
                        if f.endswith(".json.gz"):
                            out.append({"node": node, "file": f,
                                        "size": os.path.getsize(os.path.join(MON, node, f))})
                return self._json({"files": sorted(out, key=lambda x: x["file"])})
            if p == "/api/monitor/get":
                if not self._authed():
                    return
                node = re.sub(r"[^a-z0-9_-]", "", q.get("node", [""])[0])
                fn = re.sub(r"[^A-Za-z0-9._-]", "", q.get("file", [""])[0])
                path = os.path.join(MON, node, fn)
                if not os.path.isfile(path):
                    raise Api(404, "没有这个文件")
                data = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if p == "/api/models":
                if not self._authed():
                    return
                out = rows("""SELECT id,name,base_url,model_id,kind,created FROM models
                              ORDER BY created""")
                for m_ in out:
                    m_["sk_tail"] = ""
                    r_ = one("SELECT api_key FROM models WHERE id=?", (m_["id"],))
                    if r_ and r_.get("api_key"):
                        m_["sk_tail"] = "…" + r_["api_key"][-4:]
                    m_["agents"] = one("SELECT COUNT(*) n FROM agents WHERE model=? AND deleted=0",
                                       (m_["id"],))["n"]
                return self._json({"models": out})
            if p == "/api/skills":
                if not self._authed():
                    return
                ag = q.get("agent", [""])[0]
                host = (one("SELECT host FROM agents WHERE id=?", (ag,)) or {}).get("host", "")
                allsk = rows("""SELECT id,name,summary,when_to_use,version,source,origin_host
                                FROM skills WHERE origin_host=? ORDER BY id""", (host,))
                installed = set(r["skill"] for r in rows(
                    "SELECT skill FROM agent_skills WHERE agent=? AND enabled=1", (ag,))) if ag else set()
                counts = {}
                for r in rows("SELECT skill, COUNT(*) n FROM agent_skills WHERE enabled=1 GROUP BY skill"):
                    counts[r["skill"]] = r["n"]
                for k in allsk:
                    k["enabled"] = k["id"] in installed
                    k["installed_count"] = counts.get(k["id"], 0)
                return self._json({"skills": allsk, "host": host})
            if p == "/api/files":
                if not self._authed():
                    return
                ag = q.get("agent", [""])[0]
                rows_ = rows("""SELECT id,name,size,mime,ts,msg FROM files
                                WHERE kind='in' AND agent=? ORDER BY ts DESC LIMIT 30""", (ag,))
                for f in rows_:
                    f["kind"] = file_kind(f["mime"], f["name"])
                    f["url"] = "api/file?id=" + f["id"]
                return self._json({"files": rows_})
            if p == "/api/file":
                if not self._authed():
                    return
                return self._file(q.get("id", [""])[0], dl=bool(q.get("dl", [""])[0]))
            if p == "/api/daemon/agents":
                if not daemon_ok(self):
                    return self._json({"error": "forbidden"}, 403)
                mine = host_by_token(self) or ""
                ags_ = rows("SELECT id,name,cwd,prompt,model,always_on,managed_by FROM agents "
                            "WHERE deleted=0 AND host=? ORDER BY sort,id", (mine,))
                for a_ in ags_:
                    a_["model_cfg"] = model_cfg_for(a_.get("model"))
                return self._json({"host": mine, "agents": ags_,
                    "ops": rows("SELECT id,agent,op,path FROM daemon_ops "
                                "WHERE done=0 AND (host IS NULL OR host='' OR host=?) ORDER BY id",
                                (mine,))})
            if p == "/api/daemon/version":
                if not daemon_ok(self):
                    return self._json({"error": "forbidden"}, 403)
                import hashlib as _h
                data = open(DAEMON_SRC, "rb").read()
                return self._json({"sha256": _h.sha256(data).hexdigest(), "size": len(data)})
            if p == "/api/daemon/source":
                if not daemon_ok(self):
                    return self._json({"error": "forbidden"}, 403)
                data = open(DAEMON_SRC, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/x-python; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if p == "/api/daemon/poll":
                if not daemon_ok(self):
                    return self._json({"error": "forbidden"}, 403)
                return self._json(daemon_poll(float(q.get("wait", ["20"])[0]), host_by_token(self) or ""))
            if p == "/api/monitor/push.sh":
                q2 = parse_qs(u.query)
                tok = q2.get("t", [""])[0]
                if not MON_TOKEN or not hmac.compare_digest(tok, MON_TOKEN):
                    return self._json({"error": "token 不对"}, 403)
                body = PUSH_SH.replace("__TOKEN__", MON_TOKEN).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if p in ("/api/host/daemon.py", "/api/host/install.ps1"):
                q2 = parse_qs(u.query)
                name, tok = q2.get("name", [""])[0], q2.get("t", [""])[0]
                h_ = one("SELECT * FROM hosts WHERE name=?", (name,))
                if not h_ or not h_["token"] or not hmac.compare_digest(tok, h_["token"]):
                    return self._json({"error": "token 不对"}, 403)
                if p.endswith("daemon.py"):
                    data = open(DAEMON_SRC, "rb").read()
                    ctype = "text/x-python; charset=utf-8"
                else:
                    data = self._install_ps1(name, tok).encode()
                    ctype = "text/plain; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if p == "/api/host/install.sh":
                q2 = parse_qs(u.query)
                name, tok = q2.get("name", [""])[0], q2.get("t", [""])[0]
                h = one("SELECT * FROM hosts WHERE name=?", (name,))
                if not h or not h["token"] or not hmac.compare_digest(tok, h["token"]):
                    return self._json({"error": "token 不对"}, 403)
                return self._install_script(name, tok)
            if p == "/api/webver":
                try:
                    idx = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
                    m = re.search(r"app\.js\?v=(\d+)", idx)
                    v = m.group(1) if m else ""
                except OSError:
                    v = ""
                return self._json({"v": v})
            if p == "/api/health":
                return self._json({"ok": True, "ts": now()})
            self._json({"error": "not found"}, 404)
        except Api as e:
            self._json({"error": e.msg}, e.code)
        except Exception as e:
            sys.stderr.write("ERR %r\n" % e)
            self._json({"error": "服务器错误"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/api/login":
                d = self._body()
                if hmac.compare_digest(d.get("password", ""), CONF["password"]):
                    self.send_response(200)
                    body = json.dumps({"ok": True}).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Set-Cookie",
                                     "sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000" % cookie_value())
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self._json({"error": "口令不对"}, 401)
            if p == "/api/logout":
                self.send_response(200)
                self.send_header("Set-Cookie", "sid=; Path=/; Max-Age=0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not daemon_ok(self) and p.startswith("/api/daemon/"):
                return self._json({"error": "forbidden"}, 403)
            if p == "/api/daemon/poll":
                return self._json(daemon_poll(20, host_by_token(self) or ""))
            if p == "/api/daemon/dispatch":
                d = self._body()
                aid, txt = d.get("agent", ""), (d.get("text") or "").strip()
                origin = d.get("origin") or "主 agent"
                if not aid or not txt:
                    raise Api(400, "agent 与 text 必填")
                if not one("SELECT id FROM agents WHERE id=?", (aid,)):
                    raise Api(404, "没有这个 agent: %s" % aid)
                if one("SELECT id FROM jobs WHERE agent=? AND status IN('queued','running')", (aid,)):
                    return self._json({"busy": True, "agent": aid})
                cmdr = one("SELECT * FROM agents WHERE id=? AND deleted=0", (origin,))
                if cmdr:
                    n = one("SELECT COUNT(*) n FROM agents WHERE managed_by=? AND deleted=0", (origin,))["n"]
                    m = one("SELECT id FROM agents WHERE id=? AND managed_by=? AND deleted=0", (aid, origin))
                    if n > 0 and not m:
                        raise Api(403, "%s 不在 %s 的团队里" % (aid, origin))
                body = "[来自 %s] %s" % (origin, txt)
                uid = run("INSERT INTO messages(agent,role,text,ts,status) VALUES(?,?,?,?,'done')",
                          (aid, "me", body, now()))
                aidm = run("INSERT INTO messages(agent,role,text,ts,status) VALUES(?,?,?,?,'running')",
                           (aid, "agent", "", now()))
                jid = uuid.uuid4().hex[:12]
                run("INSERT INTO jobs(id,agent,user_msg,agent_msg,status,origin) VALUES(?,?,?,?,'queued',?)",
                    (jid, aid, uid, aidm, origin))
                with WAIT:
                    WAIT.notify_all()
                bump()
                return self._json({"job": jid, "agent": aid, "agent_msg": aidm})
            if p == "/api/daemon/op_done":
                d = self._body()
                run("UPDATE daemon_ops SET done=1 WHERE id=?", (d.get("id"),))
                return self._json({"ok": True})
            if p == "/api/daemon/task":
                return self._json(self.task_state(self._body().get("job", "")))
            if p == "/api/daemon/usage":
                d = self._body()
                run("""INSERT INTO usage_daily(agent, day, jobs, ms) VALUES(?,?,?,?)
                       ON CONFLICT(agent, day) DO UPDATE SET jobs=excluded.jobs, ms=excluded.ms""",
                    (d.get("agent"), d.get("day"), int(d.get("jobs") or 0), int(d.get("ms") or 0)))
                return self._json({"ok": True})
            if p == "/api/daemon/event":
                return self._json(daemon_event(self._body()))
            if p == "/api/daemon/agents":
                if not daemon_ok(self):
                    return self._json({"error": "forbidden"}, 403)
                mine = host_by_token(self) or ""
                return self._json({"host": mine, "agents": rows(
                    "SELECT id,name,cwd,prompt,model,always_on,managed_by FROM agents "
                    "WHERE deleted=0 AND host=? ORDER BY sort,id", (mine,)),
                    "ops": rows("SELECT id,agent,op,path FROM daemon_ops "
                                "WHERE done=0 AND (host IS NULL OR host='' OR host=?) ORDER BY id",
                                (mine,))})
            if p == "/api/daemon/recover":
                # 守护进程重启后，把上次遗留的 running 任务放回队列
                with DB_LOCK, db() as c:
                    c.execute("UPDATE jobs SET status='queued', started=NULL WHERE status='running'")
                    c.commit()
                return self._json({"ok": True})
            if p == "/api/daemon/hb":
                return self._json(daemon_hb(self._body()))
            if p == "/api/daemon/model_report":
                if not daemon_ok(self):
                    return self._json({"error": "forbidden"}, 403)
                d = self._body()
                host_ = d.get("host") or (host_by_token(self) or "")
                if not host_ or not d.get("base_url") or not d.get("api_key"):
                    raise Api(400, "上报内容不完整")
                base_slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9_-]", "-", host_.lower())).strip("-")
                slug = (base_slug or "host") + "-default"
                run("""INSERT INTO models(id,name,base_url,model_id,api_key,kind,max_context,created)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name, base_url=excluded.base_url,
                         model_id=excluded.model_id, api_key=excluded.api_key, kind=excluded.kind,
                         max_context=excluded.max_context""",
                    (slug, (d.get("name") or slug)[:80], d.get("base_url"), d.get("model_id"),
                     d.get("api_key"), d.get("kind") or "openai",
                     int(d.get("max_context") or 131072), now()))
                for a in rows("""SELECT id FROM agents WHERE host=? AND deleted=0
                                 AND (model IS NULL OR model='')""", (host_,)):
                    run("UPDATE agents SET model=? WHERE id=?", (slug, a["id"]))
                    run("INSERT INTO daemon_ops(agent, op, path, ts, host) VALUES(?,?,?,?,?)",
                        (a["id"], "restart", "", now(), host_))
                bump()
                return self._json({"ok": True, "model": slug})
            if p == "/api/daemon/artifact":
                return self._json(artifact_add(self._body()))
            if p == "/api/daemon/upload":
                return self._json(self._artifact_upload())
            if p == "/api/monitor/ingest":
                if not MON_TOKEN or not hmac.compare_digest(self.headers.get("X-Monitor-Token", ""), MON_TOKEN):
                    raise Api(403, "monitor token 不对")
                node = re.sub(r"[^a-z0-9_-]", "", (self.headers.get("X-Node") or "").lower())
                day = re.sub(r"[^0-9-]", "", self.headers.get("X-Date") or "")
                if not node or not day:
                    raise Api(400, "缺 X-Node / X-Date")
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    data = __import__("gzip").decompress(raw)
                except Exception:
                    raise Api(400, "不是 gzip")
                try:
                    obj = json.loads(data)
                except Exception:
                    raise Api(400, "JSON 解不开")
                d = os.path.join(MON, node)
                os.makedirs(d, exist_ok=True)
                fn = os.path.join(d, "%s-%s.json.gz" % (node, day.replace("-", "")))
                with open(fn, "wb") as f:
                    f.write(raw)
                return self._json({"ok": True, "node": node, "day": day,
                                   "bytes": len(raw), "daily": (obj.get("daily") or [{}])[0].get("requests")})
            if not self._authed():
                return
            if p == "/api/send":
                d = self._body()
                return self._json(session_send(d.get("agent", ""), d.get("text", ""), d.get("mode", "full")))
            if p == "/api/host/create":
                d = self._body()
                name = (d.get("name") or "").strip()
                if not name:
                    raise Api(400, "要填主机名")
                if one("SELECT name FROM hosts WHERE name=?", (name,)):
                    raise Api(409, "这台主机已经有了")
                tok = uuid.uuid4().hex + uuid.uuid4().hex[:8]
                run("INSERT INTO hosts(name,token,created,last_seen) VALUES(?,?,?,0)",
                    (name, tok, now()))
                return self._json({"ok": True, "name": name, "token": tok})
            if p == "/api/host/token":
                d = self._body()
                h = one("SELECT name, token FROM hosts WHERE name=?", (d.get("name", ""),))
                if not h or not h["token"]:
                    raise Api(404, "没有这台主机")
                return self._json({"name": h["name"], "token": h["token"]})
            if p == "/api/host/rotate":
                d = self._body()
                name = (d.get("name") or "").strip()
                if not one("SELECT name FROM hosts WHERE name=?", (name,)):
                    raise Api(404, "没有这台主机")
                tok = uuid.uuid4().hex + uuid.uuid4().hex[:8]
                run("UPDATE hosts SET token=? WHERE name=?", (tok, name))
                return self._json({"ok": True, "token": tok})
            if p == "/api/host/delete":
                d = self._body()
                name = (d.get("name") or "").strip()
                n = one("SELECT COUNT(*) n FROM agents WHERE host=? AND deleted=0", (name,))["n"]
                if n:
                    raise Api(409, "这台主机上还有 %d 个 agent" % n)
                run("DELETE FROM hosts WHERE name=?", (name,))
                return self._json({"ok": True})
            if p == "/api/events":
                if not self._authed():
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last = -1
                try:
                    while True:
                        with REV_COND:
                            if REV[0] == last:
                                REV_COND.wait(timeout=15)
                        cur = REV[0]
                        self.wfile.write(("data: %d\n\n" % cur).encode())
                        self.wfile.flush()
                        last = cur
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            if p == "/api/hosts":
                if not self._authed():
                    return
                hs = rows("SELECT name, last_seen, created, (token IS NOT NULL) AS has_token FROM hosts ORDER BY name")
                for h in hs:
                    h["online"] = bool(h["last_seen"] and now() - h["last_seen"] < 90)
                    h["agents"] = one("SELECT COUNT(*) n FROM agents WHERE host=? AND deleted=0",
                                      (h["name"],))["n"]
                return self._json({"hosts": hs})
            if p == "/api/agents/archived":
                return self._json({"agents": rows(
                    "SELECT id,name,host,monogram FROM agents WHERE deleted=1 ORDER BY sort,id")})
            if p == "/api/model/save":
                d = self._body()
                name = (d.get("name") or "").strip()
                base = (d.get("base_url") or "").strip().rstrip("/")
                mid = (d.get("model_id") or "").strip()
                if not name or not base or not mid:
                    raise Api(400, "名字、base url、模型名都要填")
                kind = "kimi" if (d.get("kind") or "") == "kimi" else "openai"
                def _slug(v):
                    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9_-]", "-", (v or "").lower())).strip("-")
                slug = (d.get("id") or "").strip() or _slug(mid) or _slug(name) or ("m%d" % int(now()))
                slug = slug[:40]
                old = one("SELECT api_key FROM models WHERE id=?", (slug,))
                sk = (d.get("sk") or "").strip()
                if not sk and old:
                    sk = old.get("api_key") or ""
                if not sk:
                    raise Api(400, "要填 sk")
                try:
                    mctx = int(d.get("max_context") or 0) or 131072
                except (TypeError, ValueError):
                    mctx = 131072
                run("""INSERT INTO models(id,name,base_url,model_id,api_key,kind,max_context,created)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name, base_url=excluded.base_url,
                         model_id=excluded.model_id, api_key=excluded.api_key,
                         kind=excluded.kind, max_context=excluded.max_context""",
                    (slug, name, base, mid, sk, kind, mctx, now()))
                bump()
                return self._json({"ok": True, "id": slug})
            if p == "/api/model/delete":
                d = self._body()
                mid_ = d.get("id") or ""
                n_ = one("SELECT COUNT(*) n FROM agents WHERE model=? AND deleted=0", (mid_,))["n"]
                if n_:
                    raise Api(400, "还有 %d 个 agent 在用这个模型" % n_)
                run("DELETE FROM models WHERE id=?", (mid_,))
                bump()
                return self._json({"ok": True})
            if p == "/api/agent/model":
                d = self._body()
                aid_, mid_ = d.get("agent") or "", d.get("model") or ""
                ag_ = one("SELECT id,host FROM agents WHERE id=? AND deleted=0", (aid_,))
                if not ag_:
                    raise Api(404, "没有这个 agent")
                if not mid_:
                    raise Api(400, "要选一个模型")
                if not one("SELECT id FROM models WHERE id=?", (mid_,)):
                    raise Api(404, "没有这个模型")
                if one("SELECT id FROM jobs WHERE agent=? AND status IN('queued','running')", (aid_,)):
                    raise Api(409, "这个 agent 正在跑任务，等它跑完再切换模型")
                run("UPDATE agents SET model=? WHERE id=?", (mid_, aid_))
                run("INSERT INTO daemon_ops(agent, op, path, ts, host) VALUES(?,?,?,?,?)",
                    (aid_, "restart", "", now(), ag_.get("host") or ""))
                bump()
                return self._json({"ok": True})
            if p == "/api/agent/create":
                d = self._body()
                name = (d.get("name") or "").strip()
                if not name:
                    raise Api(400, "要填名字")
                aid = (d.get("id") or "").strip() or ("a%d" % int(now()))
                if not re.fullmatch(r"[a-z0-9_-]{2,24}", aid):
                    raise Api(400, "id 只能是 2–24 位小写字母、数字、下划线或短横线")
                if one("SELECT id FROM agents WHERE id=?", (aid,)):
                    raise Api(409, "这个 id 已经存在")
                cwd = (d.get("cwd") or "").strip()   # 留空由 Daemon 按本机规则落盘
                sort = (one("SELECT COALESCE(MAX(sort),0)+1 n FROM agents") or {}).get("n", 9)
                mid_ = (d.get("model") or "").strip()
                if not mid_:
                    raise Api(400, "新建 agent 要选一个模型（先去「模型配置」里加）")
                if not one("SELECT id FROM models WHERE id=?", (mid_,)):
                    raise Api(400, "没有这个模型")
                run("""INSERT INTO agents(id,name,host,monogram,cwd,model,prompt,sort,always_on)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (aid, name, d.get("host") or "未指定主机", name[0], cwd,
                     mid_, d.get("prompt") or "",
                     sort, 1 if d.get("always_on") else 0))
                bump(); return self._json({"ok": True, "agent": aid})
            if p == "/api/agent/restore":
                d = self._body()
                run("UPDATE agents SET deleted=0 WHERE id=?", (d.get("agent", ""),))
                bump(); return self._json({"ok": True})
            if p == "/api/agent/purge":
                d = self._body()
                aid, name = d.get("agent", ""), d.get("name", "")
                ag = one("SELECT * FROM agents WHERE id=? AND deleted=1", (aid,))
                if not ag:
                    raise Api(404, "只能彻底清除已删除的 agent")
                if name.strip() != ag["name"]:
                    raise Api(400, "名字不对，没有清除")
                with DB_LOCK, db() as c:
                    c.execute("DELETE FROM messages WHERE agent=?", (aid,))
                    c.execute("DELETE FROM jobs WHERE agent=?", (aid,))
                    c.execute("DELETE FROM files WHERE agent=?", (aid,))
                    c.execute("DELETE FROM usage_daily WHERE agent=?", (aid,))
                    c.execute("DELETE FROM agents WHERE id=?", (aid,))
                    c.execute("INSERT INTO daemon_ops(agent, op, path, ts) VALUES(?,?,?,?)",
                              (aid, "purge_dir", ag["cwd"], now()))
                    c.commit()
                bump(); return self._json({"ok": True})
            if p == "/api/team":
                d = self._body()
                cmd = d.get("commander", "")
                mem = rows("""SELECT id,name,monogram,status,always_on FROM agents
                              WHERE managed_by=? AND deleted=0 ORDER BY sort,id""", (cmd,))
                cand = rows("""SELECT id,name FROM agents WHERE deleted=0 AND id!=?
                               AND managed_by IS NULL AND id NOT IN
                               (SELECT id FROM agents WHERE managed_by IS NOT NULL)
                               ORDER BY sort,id""", (cmd,))
                return self._json({"members": mem, "candidates": cand})
            if p == "/api/team/add":
                d = self._body()
                cmd, mem = d.get("commander", ""), d.get("member", "")
                if cmd == mem:
                    raise Api(400, "不能把自己加进自己团队")
                if not one("SELECT id FROM agents WHERE id=? AND deleted=0", (mem,)):
                    raise Api(404, "没有这个 agent")
                run("UPDATE agents SET managed_by=? WHERE id=?", (cmd, mem))
                bump(); return self._json({"ok": True})
            if p == "/api/team/remove":
                d = self._body()
                run("UPDATE agents SET managed_by=NULL WHERE id=? AND managed_by=?",
                    (d.get("member", ""), d.get("commander", "")))
                bump(); return self._json({"ok": True})
            if p == "/api/agent/delete":
                d = self._body()
                aid = d.get("agent", "")
                if not one("SELECT id FROM agents WHERE id=? AND deleted=0", (aid,)):
                    raise Api(404, "没有这个 agent")
                if one("SELECT id FROM jobs WHERE agent=? AND status IN('queued','running')", (aid,)):
                    raise Api(409, "它还有任务在跑，先中止再删")
                n = one("SELECT COUNT(*) n FROM agents WHERE managed_by=? AND deleted=0", (aid,))["n"]
                if n:
                    raise Api(409, "它名下还有 %d 个 agent，先把成员移出去" % n)
                run("UPDATE agents SET deleted=1, always_on=0 WHERE id=?", (aid,))
                bump(); return self._json({"ok": True})
            if p == "/api/agent/always_on":
                d = self._body()
                aid = d.get("agent", "")
                on = 1 if d.get("on") else 0
                if not one("SELECT id FROM agents WHERE id=?", (aid,)):
                    raise Api(404, "没有这个 agent")
                run("UPDATE agents SET always_on=? WHERE id=?", (on, aid))
                return self._json({"ok": True, "agent": aid, "always_on": bool(on)})
            if p == "/api/skill/create":
                d = self._body()
                name = (d.get("name") or "").strip()
                if not name:
                    raise Api(400, "技能要有名字")
                sid = (d.get("id") or "").strip() or ("s%d" % int(now()))
                if one("SELECT id FROM skills WHERE id=?", (sid,)):
                    raise Api(409, "技能 id 已存在")
                run("""INSERT INTO skills(id,name,summary,when_to_use,body,version,created,source,origin_host)
                       VALUES(?,?,?,?,?,?,?,'hub','')""",
                    (sid, name, d.get("summary") or "", d.get("when") or "",
                     d.get("body") or "", d.get("version") or "1", now()))
                bump()
                return self._json({"ok": True, "id": sid})
            if p == "/api/skill/op":
                d = self._body()
                op, agent_, sid = d.get("op"), d.get("agent") or "", d.get("skill") or ""
                ag0 = one("SELECT host FROM agents WHERE id=? AND deleted=0", (agent_,)) if agent_ else None
                host0 = (ag0 or {}).get("host", "")
                sk = one("SELECT id,origin_host FROM skills WHERE id=? AND origin_host=?", (sid, host0)) \
                     if host0 else one("SELECT id,origin_host FROM skills WHERE id=?", (sid,))
                if not sk:
                    raise Api(404, "没有这个技能")
                if op == "delete":
                    n = one("""SELECT COUNT(*) n FROM agent_skills s JOIN agents a ON a.id=s.agent
                               WHERE s.skill=? AND s.enabled=1 AND a.host=?""",
                            (sid, sk.get("origin_host") or ""))["n"]
                    if n:
                        raise Api(400, "还有 %d 个 agent 装着它，先卸载完再删" % n)
                    run("INSERT INTO daemon_ops(agent, op, path, ts, host) VALUES(?,?,?,?,?)",
                        ("", "skill_delete", sid, now(), sk.get("origin_host") or ""))
                    bump()
                    return self._json({"ok": True})
                if op not in ("install", "uninstall"):
                    raise Api(400, "op 只能是 install / uninstall / delete")
                ag = one("SELECT id,host FROM agents WHERE id=? AND deleted=0", (agent_,))
                if not ag:
                    raise Api(404, "没有这个 agent")
                if (ag.get("host") or "") != (sk.get("origin_host") or ""):
                    raise Api(400, "这个技能不在该 agent 所在机器的库里")
                run("INSERT INTO daemon_ops(agent, op, path, ts, host) VALUES(?,?,?,?,?)",
                    (agent_, "skill_" + op, sid, now(), ag.get("host") or ""))
                bump()
                return self._json({"ok": True})
            if p == "/api/skill/bind":
                d = self._body()
                ag, sk = d.get("agent", ""), d.get("skill", "")
                if not one("SELECT id FROM agents WHERE id=? AND deleted=0", (ag,)):
                    raise Api(404, "没有这个 agent")
                if not one("SELECT id FROM skills WHERE id=?", (sk,)):
                    raise Api(404, "没有这个技能")
                run("""INSERT INTO agent_skills(agent,skill,enabled) VALUES(?,?,?)
                       ON CONFLICT(agent,skill) DO UPDATE SET enabled=excluded.enabled""",
                    (ag, sk, 1 if d.get("on") else 0))
                bump()
                return self._json({"ok": True})
            if p == "/api/skill/delete":
                d = self._body()
                sk = d.get("skill", "")
                row_ = one("SELECT source,origin_host FROM skills WHERE id=?", (sk,))
                sk_host = (row_ or {}).get("origin_host") or ""
                if row_ and (row_.get("source") or "hub") == "local":
                    host = row_.get("origin_host") or "某台机器"
                    raise Api(400, "这个技能是从「%s」的磁盘同步来的，在中枢删除会被重新同步。"
                                   "要彻底删，请到那台机器的 ~/.kimi-code/skills/ 删掉对应目录；"
                                   "只想让这个 agent 不用它，点「停用」即可。" % host)
                run("DELETE FROM skills WHERE id=? AND origin_host=?", (sk, sk_host))
                run("DELETE FROM agent_skills WHERE skill=? AND agent IN "
                    "(SELECT id FROM agents WHERE host=?)", (sk, sk_host))
                bump()
                return self._json({"ok": True})
            if p == "/api/file/delete":
                d = self._body()
                f = one("SELECT * FROM files WHERE id=? AND kind='in'", (d.get("id", ""),))
                if not f:
                    raise Api(404, "没有这个附件")
                run("DELETE FROM files WHERE id=?", (f["id"],))
                if f["stored"] and os.path.exists(f["stored"]):
                    try: os.remove(f["stored"])
                    except OSError: pass
                bump()
                return self._json({"ok": True})
            if p == "/api/cancel":
                d = self._body()
                return self._json(job_cancel(d.get("agent", "")))
            if p == "/api/upload":
                return self._json(upload(self, (parse_qs(u.query).get("agent", [""])[0])))
            self._json({"error": "not found"}, 404)
        except Api as e:
            self._json({"error": e.msg}, e.code)
        except Exception as e:
            sys.stderr.write("ERR %r\n" % e)
            self._json({"error": "服务器错误"}, 500)

    # ---- 文件 ----
    def _install_ps1(self, host, token):
        """Windows 版安装脚本：纯 ASCII，避免 PowerShell 5.1 的中文编码坑。"""
        hub = CONF.get("public_url", "https://nykj.store/hub")
        return r"""$ErrorActionPreference = "Stop"
$HostName = "%(host)s"
$Hub = "%(hub)s"
$Token = "%(token)s"
$Root = Join-Path $env:USERPROFILE "aihub"
$Dir = Join-Path $Root "daemon"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $Dir,$LogDir | Out-Null

Write-Host "==> download daemon.py"
$url = "$Hub/api/host/daemon.py?name=" + [uri]::EscapeDataString($HostName) + "&t=$Token"
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile (Join-Path $Dir "daemon.py")

$cfg = @{ hub = $Hub; token = $Token; name = $HostName; log_dir = $LogDir } | ConvertTo-Json
Set-Content -Path (Join-Path $Dir "config.json") -Value $cfg -Encoding ASCII

$py = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py.exe -ErrorAction SilentlyContinue }
if (-not $py) { Write-Host "!! Python 3.10+ not found. Install Python first, then re-run."; exit 1 }
$pyPath = $py.Source
if ($py.Name -eq "py.exe") {
  $cand = Join-Path (Split-Path $pyPath) "python.exe"
  if (Test-Path $cand) { $pyPath = $cand }
}
$pyw = $pyPath -replace "python\.exe$", "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $pyPath }

$kimi = Get-Command kimi -ErrorAction SilentlyContinue
if (-not $kimi) { Write-Host "!! 'kimi' not found in PATH. Daemon will register but agents cannot run until kimi is on PATH." }

$tr = '"' + $pyw + '" "' + (Join-Path $Dir "daemon.py") + '"'
schtasks /End /TN "AIHubDaemon" 2>$null | Out-Null
Start-Sleep -Seconds 1
schtasks /Create /SC ONLOGON /TN "AIHubDaemon" /TR $tr /F | Out-Null
schtasks /Run /TN "AIHubDaemon" | Out-Null
Start-Sleep -Seconds 3
Write-Host "==> installed. task AIHubDaemon registered (onlogon) and started."
Write-Host "    config: $Dir\config.json    logs: $LogDir"
""" % {"host": host, "hub": hub, "token": token}

    def _install_script(self, host, token):
        import base64 as b64
        src = open(DAEMON_SRC, "rb").read()
        payload = b64.b64encode(src).decode()
        sh = """#!/usr/bin/env bash
set -e
HOST_NAME="%(host)s"
HUB="%(hub)s"
TOKEN="%(token)s"
DIR="$HOME/aihub/daemon"
echo "==> 检查环境"
command -v python3 >/dev/null || { echo "缺 python3"; exit 1; }
command -v kimi >/dev/null || echo "!! 没找到 kimi 命令，Daemon 装好了也跑不了模型，请先安装并登录 kimi"
mkdir -p "$DIR" "$HOME/aihub/logs"
echo "==> 写入 daemon"
echo '%(payload)s' | base64 -d > "$DIR/daemon.py"
cat > "$DIR/config.json" <<JSON
{"hub": "$HUB", "token": "$TOKEN", "name": "$HOST_NAME", "log_dir": "$HOME/aihub/logs"}
JSON
chmod +x "$DIR/daemon.py"
echo "==> 安装 systemd 服务"
sudo tee /etc/systemd/system/aihub-daemon.service >/dev/null <<UNIT
[Unit]
Description=AI Hub Daemon
After=network-online.target
[Service]
Type=simple
User=$USER
WorkingDirectory=$DIR
Environment=HOME=$HOME
Environment=PATH=$(dirname "$(command -v kimi)"):/usr/local/bin:/usr/bin:/bin
ExecStart=$(command -v python3) $DIR/daemon.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now aihub-daemon
sleep 2
systemctl is-active aihub-daemon && echo "==> 已上线：$HOST_NAME" || echo "!! 启动失败，看 journalctl -u aihub-daemon"
""" % {"host": host, "hub": CONF.get("public_url", "https://nykj.store/hub"), "token": token, "payload": payload}
        body = sh.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def task_state(self, jid):
        j = one("""SELECT j.*, a.name as agent_name, m.text as reply, m.status as msg_status
                   FROM jobs j LEFT JOIN agents a ON a.id=j.agent
                   LEFT JOIN messages m ON m.id=j.agent_msg WHERE j.id=?""", (jid,))
        if not j:
            raise Api(404, "没有这个任务")
        return {"id": j["id"], "agent": j["agent"], "agent_name": j["agent_name"],
                "status": j["status"], "error": j["error"], "origin": j["origin"],
                "started": j["started"], "ended": j["ended"],
                "text": j["reply"] or "", "msg_status": j["msg_status"]}

    def _file(self, fid, dl=False):
        f = one("SELECT * FROM files WHERE id=?", (fid,))
        if not f or not f["stored"] or not os.path.exists(f["stored"]):
            return self._json({"error": "文件不在中枢，需要从机器上取"}, 404)
        data = open(f["stored"], "rb").read()
        ctype = f["mime"] or "application/octet-stream"
        if (ctype.startswith("text/") or ctype in ("application/json", "application/xml",
                                                   "application/javascript", "application/x-javascript")) \
                and "charset" not in ctype:
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         "%s; filename*=UTF-8''%s" % ("attachment" if dl else "inline",
                                                      __import__("urllib.parse").parse.quote(f["name"])))
        self.end_headers()
        self.wfile.write(data)

    def _artifact_upload(self):
        # Daemon 把产出内容推上来（小文件自动、大文件按需）
        fid = self.headers.get("X-File-Id") or ""
        n = int(self.headers.get("Content-Length") or 0)
        f = one("SELECT * FROM files WHERE id=?", (fid,))
        if not f:
            raise Api(404, "没有这个产出")
        path = os.path.join(FILES, fid)
        with open(path, "wb") as fh:
            left = n
            while left > 0:
                b = self.rfile.read(min(left, 1 << 20))
                if not b:
                    break
                fh.write(b)
                left -= len(b)
        run("UPDATE files SET stored=?, size=? WHERE id=?", (path, os.path.getsize(path), fid))
        return {"ok": True}

    # ---- 静态 ----
    def _static(self, rel):
        rel = rel.split("?")[0]
        if rel in ("", "/"):
            rel = "index.html"
        path = os.path.realpath(os.path.join(STATIC, rel.lstrip("/")))
        if not path.startswith(os.path.realpath(STATIC)) or not os.path.isfile(path):
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = open(path, "rb").read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def cleanup_loop():
    """每天清一次过期数据：消息 180 天、产出 30 天、暂存附件 7 天。"""
    while True:
        try:
            cut_msg = now() - 180 * 86400
            cut_art = now() - 30 * 86400
            cut_in = now() - 7 * 86400
            with DB_LOCK, db() as c:
                c.execute("DELETE FROM messages WHERE ts < ? AND role='agent'", (cut_msg,))
                dead = [r for r in c.execute(
                    "SELECT id,stored FROM files WHERE kind='out' AND ts < ?", (cut_art,)).fetchall()]
                dead += [r for r in c.execute(
                    "SELECT id,stored FROM files WHERE kind='in' AND ts < ? AND msg IS NULL", (cut_in,)).fetchall()]
                for fid, stored in dead:
                    c.execute("DELETE FROM files WHERE id=?", (fid,))
                    for p_ in {stored or "", os.path.join(FILES, fid)}:
                        if p_ and os.path.exists(p_):
                            try: os.remove(p_)
                            except OSError: pass
                c.commit()
            sys.stderr.write("cleanup: removed %d files\n" % len(dead))
        except Exception as e:
            sys.stderr.write("cleanup failed: %r\n" % e)
        time.sleep(6 * 3600)


def main():
    init()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    port = int(CONF.get("port", 8791))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print("aihub hub on 127.0.0.1:%d, data=%s" % (port, DATA), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
