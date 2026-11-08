"""双池存储。

main.db  —— 主池（私有）：会话、消息、摘要、接力记录。按 participant_id 分区，只有控制台读写。
arena.db —— 对抗池（Agent 可经授权读取）：发布条目、Agent 身份、对局、公开轮次、收件箱、读取申请、票据、审计。

两个库是两个文件、两把密钥，没有共享表和外键。正文字段一律 Fernet(AES) 加密。

SQL 约定：每条 SQL 以字面量写在执行点、参数一律 ? 占位；_DB 刻意不提供
“接收 SQL 字符串”的通用执行入口，杜绝动态拼 SQL 的形态。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class _DB:
    """sqlite 薄包装：统一加锁、提交与回滚；加解密在这里，SQL 字面量在调用点。"""

    def __init__(self, path: str | Path, fernet: Fernet, schema: str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.f = fernet
        with self.lock:
            self.conn.executescript(schema)   # schema 为模块级字面量
            self.conn.commit()

    # ---------------- 加解密
    def enc(self, text: str | None) -> bytes | None:
        return None if text is None else self.f.encrypt(text.encode("utf-8"))

    def dec(self, blob: bytes | None) -> str | None:
        return None if blob is None else self.f.decrypt(blob).decode("utf-8")

    # ---------------- 事务
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """with db.write() as conn: conn.execute("字面量 SQL", (参数,))——统一加锁、提交、异常回滚。"""
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            yield self.conn


# ====================================================================== 主池
MAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, title TEXT, participant_id TEXT DEFAULT 'user',
  current_platform TEXT, chat_ref TEXT, chain TEXT DEFAULT '[]',
  summary_enc BLOB, summarized_upto INTEGER DEFAULT 0, created_at REAL,
  account TEXT DEFAULT 'main');
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
  platform TEXT, content_enc BLOB, ts REAL);
CREATE TABLE IF NOT EXISTS handoffs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, from_platform TEXT,
  to_platform TEXT, to_account TEXT, reason TEXT, ts REAL);
"""


def _migrate_main(conn: sqlite3.Connection) -> None:
    """旧库补列（表名/列名均为本模块常量，不来自外部输入）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "account" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN account TEXT DEFAULT 'main'")
    hcols = {r["name"] for r in conn.execute("PRAGMA table_info(handoffs)").fetchall()}
    if "to_account" not in hcols:
        conn.execute("ALTER TABLE handoffs ADD COLUMN to_account TEXT")


@dataclass
class Session:
    id: str
    title: str
    participant_id: str
    current_platform: str
    chat_ref: str | None
    chain: list[str]
    summary: str | None
    summarized_upto: int
    account: str = "main"


class MainPool:
    def __init__(self, data_dir: str | Path, fernet: Fernet):
        self.db = _DB(Path(data_dir) / "main.db", fernet, MAIN_SCHEMA)
        with self.db.write() as conn:
            _migrate_main(conn)

    def create_session(self, title: str, platform: str, participant_id: str = "user",
                       account: str = "main") -> str:
        sid = _id("s")
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO sessions(id,title,participant_id,current_platform,chain,created_at,account) "
                "VALUES(?,?,?,?,?,?,?)",
                (sid, title, participant_id, platform, json.dumps([platform]), _now(), account),
            )
        return sid

    def get_session(self, sid: str) -> Session:
        with self.db.read() as conn:
            r = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not r:
            raise KeyError(sid)
        return Session(r["id"], r["title"], r["participant_id"], r["current_platform"], r["chat_ref"],
                       json.loads(r["chain"]), self.db.dec(r["summary_enc"]), r["summarized_upto"],
                       r["account"] or "main")

    def list_sessions(self, participant_id: str = "user") -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT id,title,current_platform,chain,created_at FROM sessions "
                                "WHERE participant_id=? ORDER BY created_at DESC", (participant_id,)).fetchall()
        return [dict(r) | {"chain": json.loads(r["chain"])} for r in rows]

    def set_chat_ref(self, sid: str, chat_ref: str | None) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE sessions SET chat_ref=? WHERE id=?", (chat_ref, sid))

    def switch_platform(self, sid: str, new_platform: str, reason: str,
                        new_account: str | None = None) -> None:
        with self.db.write() as conn:
            r = conn.execute("SELECT current_platform, chain FROM sessions WHERE id=?", (sid,)).fetchone()
            if not r:
                raise KeyError(sid)
            chain = json.loads(r["chain"])
            if not chain or chain[-1] != new_platform:   # 同平台换账号不改接力链
                chain = chain + [new_platform]
            conn.execute("UPDATE sessions SET current_platform=?, account=?, chat_ref=NULL, chain=? WHERE id=?",
                         (new_platform, new_account or "main", json.dumps(chain), sid))
            conn.execute("INSERT INTO handoffs(session_id,from_platform,to_platform,to_account,reason,ts) "
                         "VALUES(?,?,?,?,?,?)",
                         (sid, r["current_platform"], new_platform, new_account or "main", reason, _now()))

    def add_message(self, sid: str, role: str, platform: str, content: str) -> int:
        with self.db.write() as conn:
            cur = conn.execute("INSERT INTO messages(session_id,role,platform,content_enc,ts) VALUES(?,?,?,?,?)",
                               (sid, role, platform, self.db.enc(content), _now()))
            return int(cur.lastrowid)

    def messages(self, sid: str, after_id: int = 0, limit: int | None = None) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT id,role,platform,content_enc,ts FROM messages "
                                "WHERE session_id=? AND id>? ORDER BY id", (sid, after_id)).fetchall()
        out = [{"id": r["id"], "role": r["role"], "platform": r["platform"],
                "content": self.db.dec(r["content_enc"]), "ts": r["ts"]} for r in rows]
        return out[-limit:] if limit else out

    def set_summary(self, sid: str, summary: str, upto_id: int) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE sessions SET summary_enc=?, summarized_upto=? WHERE id=?",
                         (self.db.enc(summary), upto_id, sid))

    def handoffs(self, sid: str) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM handoffs WHERE session_id=? ORDER BY id", (sid,)).fetchall()
        return [dict(r) for r in rows]


# ====================================================================== 对抗池
ARENA_SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, title TEXT, content_enc BLOB, audience TEXT DEFAULT '*', published_at REAL);
CREATE TABLE IF NOT EXISTS agents(
  participant_id TEXT PRIMARY KEY, display_name TEXT, token_hash TEXT UNIQUE, created_at REAL, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS arenas(
  id TEXT PRIMARY KEY, mode TEXT, topic TEXT, rules TEXT, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS seats(
  arena_id TEXT, participant_id TEXT, side TEXT, kind TEXT, ord INTEGER, platform TEXT,
  PRIMARY KEY(arena_id, participant_id));
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, arena_id TEXT, round INTEGER, participant_id TEXT,
  side TEXT, content_enc BLOB, ts REAL);
CREATE TABLE IF NOT EXISTS inbox(
  id TEXT PRIMARY KEY, arena_id TEXT, round INTEGER, participant_id TEXT, prompt_enc BLOB,
  answer_enc BLOB, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS read_requests(
  id TEXT PRIMARY KEY, participant_id TEXT, scope_type TEXT, scope TEXT, reason TEXT,
  status TEXT, created_at REAL, decided_at REAL);
CREATE TABLE IF NOT EXISTS tickets(
  request_id TEXT PRIMARY KEY, expires_at REAL, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, detail TEXT);
"""


class PoolError(Exception):
    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ArenaPool:
    def __init__(self, data_dir: str | Path, fernet: Fernet, ticket_ttl: int = 300):
        self.db = _DB(Path(data_dir) / "arena.db", fernet, ARENA_SCHEMA)
        self.ticket_ttl = ticket_ttl

    # ---------------- 审计
    def audit(self, actor: str, action: str, detail: Any = "") -> None:
        with self.db.write() as conn:
            conn.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
                         (_now(), actor, action, json.dumps(detail, ensure_ascii=False)))

    def audit_log(self, limit: int = 200) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- Agent 身份
    def create_agent(self, name: str) -> tuple[str, str]:
        pid = f"agent:{name}"
        token = secrets.token_urlsafe(32)
        with self.db.write() as conn:
            conn.execute("INSERT OR REPLACE INTO agents(participant_id,display_name,token_hash,created_at,revoked) "
                         "VALUES(?,?,?,?,0)", (pid, name, hash_token(token), _now()))
        self.audit("user", "create_agent", pid)
        return pid, token

    def revoke_agent(self, pid: str) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE agents SET revoked=1 WHERE participant_id=?", (pid,))
        self.audit("user", "revoke_agent", pid)

    def agent_by_token(self, token: str) -> str | None:
        with self.db.read() as conn:
            r = conn.execute("SELECT participant_id FROM agents WHERE token_hash=? AND revoked=0",
                             (hash_token(token),)).fetchone()
        return r["participant_id"] if r else None

    def list_agents(self) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT participant_id,display_name,created_at,revoked FROM agents").fetchall()
        return [dict(r) for r in rows]

    # ---------------- 发布条目（只能由控制台调用）
    def publish_item(self, title: str, content: str, audience: str = "*") -> str:
        iid = _id("ap")
        with self.db.write() as conn:
            conn.execute("INSERT INTO items(id,title,content_enc,audience,published_at) VALUES(?,?,?,?,?)",
                         (iid, title, self.db.enc(content), audience, _now()))
        self.audit("user", "publish_item", {"id": iid, "title": title, "audience": audience})
        return iid

    def delete_item(self, iid: str) -> None:
        with self.db.write() as conn:
            conn.execute("DELETE FROM items WHERE id=?", (iid,))
        self.audit("user", "delete_item", iid)

    def _visible(self, audience: str, pid: str) -> bool:
        return audience == "*" or pid in [a.strip() for a in audience.split(",")]

    def list_items(self, pid: str | None = None) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT id,title,audience,published_at FROM items ORDER BY published_at DESC").fetchall()
        return [dict(r) for r in rows if pid is None or self._visible(r["audience"], pid)]

    # ---------------- 对局
    def create_arena(self, mode: str, topic: str, rules: str, seats: list[dict]) -> str:
        aid = _id("ar")
        with self.db.write() as conn:
            conn.execute("INSERT INTO arenas(id,mode,topic,rules,status,created_at) VALUES(?,?,?,?,?,?)",
                         (aid, mode, topic, rules, "open", _now()))
            for i, s in enumerate(seats):
                conn.execute("INSERT INTO seats(arena_id,participant_id,side,kind,ord,platform) VALUES(?,?,?,?,?,?)",
                             (aid, s["participant_id"], s["side"], s["kind"], i, s.get("platform")))
        self.audit("user", "create_arena", {"id": aid, "mode": mode})
        return aid

    def arena(self, aid: str) -> dict:
        with self.db.read() as conn:
            a = conn.execute("SELECT * FROM arenas WHERE id=?", (aid,)).fetchone()
            seats = [dict(r) for r in conn.execute(
                "SELECT * FROM seats WHERE arena_id=? ORDER BY ord", (aid,)).fetchall()]
        if not a:
            raise PoolError("NOT_FOUND")
        return dict(a) | {"seats": seats}

    def list_arenas(self) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM arenas ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def is_seated(self, aid: str, pid: str) -> bool:
        with self.db.read() as conn:
            r = conn.execute("SELECT 1 FROM seats WHERE arena_id=? AND participant_id=?", (aid, pid)).fetchone()
        return r is not None

    def add_turn(self, aid: str, rnd: int, pid: str, side: str, content: str) -> None:
        with self.db.write() as conn:
            conn.execute("INSERT INTO turns(arena_id,round,participant_id,side,content_enc,ts) VALUES(?,?,?,?,?,?)",
                         (aid, rnd, pid, side, self.db.enc(content), _now()))

    def turns(self, aid: str) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM turns WHERE arena_id=? ORDER BY id", (aid,)).fetchall()
        return [{"round": r["round"], "participant_id": r["participant_id"], "side": r["side"],
                 "content": self.db.dec(r["content_enc"]), "ts": r["ts"]} for r in rows]

    def last_round(self, aid: str) -> int:
        with self.db.read() as conn:
            r = conn.execute("SELECT MAX(round) m FROM turns WHERE arena_id=?", (aid,)).fetchone()
        return int(r["m"] or 0)

    # ---------------- 收件箱（Agent 对局用）
    def post_inbox(self, aid: str, rnd: int, pid: str, prompt: str) -> str:
        bid = _id("ib")
        with self.db.write() as conn:
            conn.execute("INSERT INTO inbox(id,arena_id,round,participant_id,prompt_enc,status,created_at) "
                         "VALUES(?,?,?,?,?,?,?)", (bid, aid, rnd, pid, self.db.enc(prompt), "waiting", _now()))
        return bid

    def inbox_answer(self, bid: str) -> str | None:
        with self.db.read() as conn:
            r = conn.execute("SELECT answer_enc,status FROM inbox WHERE id=?", (bid,)).fetchone()
        return self.db.dec(r["answer_enc"]) if r and r["status"] == "answered" else None

    def submit_turn(self, pid: str, aid: str, content: str) -> None:
        with self.db.write() as conn:
            if not conn.execute("SELECT 1 FROM seats WHERE arena_id=? AND participant_id=?",
                                (aid, pid)).fetchone():
                raise PoolError("NOT_SEATED")
            r = conn.execute("SELECT id FROM inbox WHERE arena_id=? AND participant_id=? AND status='delivered' "
                             "ORDER BY created_at DESC", (aid, pid)).fetchone()
            if not r:
                raise PoolError("NO_OPEN_TURN", "先经授权读取本轮题目，再提交")
            conn.execute("UPDATE inbox SET answer_enc=?, status='answered' WHERE id=?",
                         (self.db.enc(content), r["id"]))
        self.audit(pid, "submit_turn", {"arena_id": aid, "chars": len(content)})

    # ---------------- 读取申请与授权
    def create_request(self, pid: str, scope_type: str, scope: dict, reason: str) -> str:
        if not reason or not reason.strip():
            raise PoolError("REASON_REQUIRED")
        with self.db.read() as conn:
            if scope_type == "items":
                ids = scope.get("item_ids") or []
                if not ids:
                    raise PoolError("NOT_IN_POOL")
                for iid in ids:
                    r = conn.execute("SELECT audience FROM items WHERE id=?", (iid,)).fetchone()
                    if not r or not self._visible(r["audience"], pid):
                        raise PoolError("NOT_IN_POOL", iid)
            elif scope_type == "turn":
                if not conn.execute("SELECT 1 FROM seats WHERE arena_id=? AND participant_id=?",
                                    (scope.get("arena_id", ""), pid)).fetchone():
                    raise PoolError("NOT_SEATED")
            elif scope_type == "session":
                # 会话是否真实存在由控制台层校验（双池无共享表，这里只查形态）
                if not scope.get("session_id"):
                    raise PoolError("BAD_SCOPE")
            else:
                raise PoolError("BAD_SCOPE")
            recent = conn.execute("SELECT COUNT(*) c FROM read_requests WHERE participant_id=? AND created_at>?",
                                  (pid, _now() - 60)).fetchone()
            if recent["c"] >= 10:
                raise PoolError("RATE_LIMITED")
        rid = _id("rq")
        with self.db.write() as conn:
            conn.execute("INSERT INTO read_requests(id,participant_id,scope_type,scope,reason,status,created_at) "
                         "VALUES(?,?,?,?,?,?,?)", (rid, pid, scope_type, json.dumps(scope), reason, "pending", _now()))
        self.audit(pid, "request_read", {"id": rid, "scope_type": scope_type, "scope": scope, "reason": reason})
        return rid

    def pending_requests(self) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM read_requests WHERE status='pending' ORDER BY created_at").fetchall()
        out = []
        for r in rows:
            d = dict(r) | {"scope": json.loads(r["scope"])}
            if d["scope_type"] == "items":
                d["titles"] = [x["title"] for x in self.list_items() if x["id"] in d["scope"]["item_ids"]]
            out.append(d)
        return out

    def decide(self, rid: str, approve: bool, actor: str = "user") -> None:
        with self.db.write() as conn:
            r = conn.execute("SELECT status FROM read_requests WHERE id=?", (rid,)).fetchone()
            if not r or r["status"] != "pending":
                raise PoolError("NOT_PENDING")
            conn.execute("UPDATE read_requests SET status=?, decided_at=? WHERE id=?",
                         ("approved" if approve else "denied", _now(), rid))
            if approve:
                conn.execute("INSERT INTO tickets(request_id,expires_at,used) VALUES(?,?,0)",
                             (rid, _now() + self.ticket_ttl))
        self.audit(actor, "approve" if approve else "deny", rid)

    def approve_round(self, aid: str) -> int:
        n = 0
        for r in self.pending_requests():
            if r["scope_type"] == "turn" and r["scope"].get("arena_id") == aid:
                self.decide(r["id"], True, actor="user(batch)")
                n += 1
        return n

    def check_request(self, pid: str, rid: str) -> str:
        with self.db.read() as conn:
            r = conn.execute("SELECT status FROM read_requests WHERE id=? AND participant_id=?", (rid, pid)).fetchone()
        if not r:
            raise PoolError("NOT_FOUND")
        if r["status"] == "approved":
            with self.db.read() as conn:
                t = conn.execute("SELECT expires_at,used FROM tickets WHERE request_id=?", (rid,)).fetchone()
            if t and (t["used"] or t["expires_at"] < _now()):
                return "expired"
        return r["status"]

    def fetch(self, pid: str, rid: str) -> list[dict] | dict:
        with self.db.write() as conn:
            r = conn.execute("SELECT scope_type,scope,status FROM read_requests WHERE id=? AND participant_id=?",
                             (rid, pid)).fetchone()
            if not r or r["status"] != "approved":
                raise PoolError("NOT_APPROVED")
            t = conn.execute("SELECT expires_at,used FROM tickets WHERE request_id=?", (rid,)).fetchone()
            if not t:
                raise PoolError("NOT_APPROVED")
            if t["used"]:
                raise PoolError("TICKET_USED")
            if t["expires_at"] < _now():
                raise PoolError("TICKET_EXPIRED")
            conn.execute("UPDATE tickets SET used=1 WHERE request_id=?", (rid,))
            scope = json.loads(r["scope"])
        self.audit(pid, "fetch", rid)
        if r["scope_type"] == "session":
            # 只回标记；状态/摘要预览由控制台层从主池组装（仍受本次批准约束）
            return {"scope_type": "session", "session_id": scope.get("session_id")}
        if r["scope_type"] == "items":
            out = []
            for iid in scope["item_ids"]:
                with self.db.read() as conn:
                    it = conn.execute("SELECT id,title,content_enc FROM items WHERE id=?", (iid,)).fetchone()
                if it:
                    out.append({"id": it["id"], "title": it["title"], "content": self.db.dec(it["content_enc"])})
            return out
        with self.db.write() as inbox_conn:
            ib = inbox_conn.execute("SELECT * FROM inbox WHERE arena_id=? AND participant_id=? AND status IN "
                                    "('waiting','delivered') ORDER BY created_at DESC",
                                    (scope["arena_id"], pid)).fetchone()
            if not ib:
                return {"arena_id": scope["arena_id"], "prompt": None, "note": "当前没有轮到你的发言"}
            inbox_conn.execute("UPDATE inbox SET status='delivered' WHERE id=?", (ib["id"],))
        return {"arena_id": scope["arena_id"], "round": ib["round"], "prompt": self.db.dec(ib["prompt_enc"])}
