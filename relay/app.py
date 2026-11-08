"""控制台后端。

两类接口，两种身份：
  /api/*    控制台（你）  —— 需要启动时生成的控制台令牌（cookie 或 X-Console-Token）
  /agent/*  Agent（经 MCP）—— 需要 Agent 令牌（Bearer），且只能碰对抗池；读取一律需你批准
"""
from __future__ import annotations

import asyncio
import os
import secrets
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import adapters
from adapters.spec import AdapterError

from .arena import ArenaRunner
from .browser import ChromeManager
from .config import APP_VERSION, data_home, load_config, platform_accounts, save_config
from .crypto import CRED_ARENA, CRED_MAIN, get_api_key, get_fernet, get_platform_credential
from .relay_engine import RelayEngine
from .storage import ArenaPool, MainPool, PoolError
from .summarizer import Summarizer

CFG = load_config()
CONSOLE_TOKEN = secrets.token_urlsafe(24)          # 写进 cookie 的长期令牌
_BOOTSTRAP_TOKEN: str | None = secrets.token_urlsafe(24)   # 只用于启动时换 cookie，一次即废
STATIC = Path(__file__).with_name("static")


class State:
    main: MainPool
    arena: ArenaPool
    browser: ChromeManager
    engine: RelayEngine
    runner: ArenaRunner
    round_tasks: dict[str, asyncio.Task] = {}


S = State()


def _auto_open_url(cfg) -> str | None:
    """返回启动后应自动打开的控制台地址；不该自动打开时返回 None。"""
    if not cfg.get("auto_open_browser", True):
        return None
    if os.environ.get("RELAY_NO_OPEN") == "1":      # 脚本/测试等程序化启动可显式关闭
        return None
    if not _BOOTSTRAP_TOKEN:                        # 已经用掉（一次性）
        return None
    host = cfg["host"]
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{int(cfg['port'])}/?token={_BOOTSTRAP_TOKEN}"


def _schedule_auto_open(cfg) -> None:
    url = _auto_open_url(cfg)
    if not url:
        return
    # 延迟 1.2s：等 uvicorn 完成绑定并开始服务，避免浏览器抢跑打到未就绪端口
    asyncio.get_running_loop().call_later(1.2, lambda: webbrowser.open(url))


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.main = MainPool(CFG["data_dir"], get_fernet(CRED_MAIN))
    S.arena = ArenaPool(CFG["data_dir"], get_fernet(CRED_ARENA), CFG["approvals"]["ticket_ttl_seconds"])
    S.browser = ChromeManager(CFG)
    S.engine = RelayEngine(CFG, S.main, S.browser, Summarizer(CFG, S.main, get_api_key()))
    S.runner = ArenaRunner(CFG, S.main, S.arena, S.engine)
    if S.browser.backend is None:
        browser_desc = "自建 Chrome + 独立 profile（指纹不隔离）"
    else:
        browser_desc = f"指纹浏览器 {S.browser.backend_name}（每个平台账号一个环境）"
    print(f"\n  Relay Console v{APP_VERSION}", flush=True)
    print(f"  浏览器承载: {browser_desc}", flush=True)
    url = f"http://{CFG['host']}:{CFG['port']}/?token={_BOOTSTRAP_TOKEN}"
    print(f"  控制台地址（一次性，打开后即失效）: {url}\n", flush=True)
    try:
        (data_home() / "console_url.txt").write_text(url, encoding="utf-8")
    except OSError:
        pass
    _schedule_auto_open(CFG)
    yield
    await S.browser.close()


app = FastAPI(title="Relay Console", lifespan=lifespan)
# 只接受本机 Host，防 DNS rebinding 把控制台暴露给恶意域名
app.add_middleware(TrustedHostMiddleware,
                   allowed_hosts=["127.0.0.1", "localhost", "::1", "[::1]"])


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    # 脚本只允许同源外部文件（前端已无内联事件处理器）；样式仍允许内联
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.exception_handler(KeyError)
async def _key_err(_, e: KeyError):
    return JSONResponse({"error": "NOT_FOUND", "detail": str(e)}, status_code=404)


@app.exception_handler(PoolError)
async def _pool_err(_, e: PoolError):
    return JSONResponse({"error": e.code, "detail": str(e)}, status_code=400)


@app.exception_handler(AdapterError)
async def _adapter_err(_, e: AdapterError):
    return JSONResponse({"error": e.code, "detail": str(e)}, status_code=502)


# ---------------------------------------------------------------- 身份
def require_console(request: Request, x_console_token: str | None = Header(None)):
    tok = x_console_token or request.cookies.get("console_token")
    if not tok or not secrets.compare_digest(tok, CONSOLE_TOKEN):
        raise HTTPException(401, "console token required")


def require_agent(authorization: str | None = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "agent token required")
    pid = S.arena.agent_by_token(authorization[7:])
    if not pid:
        raise HTTPException(401, "invalid agent token")
    return pid


@app.get("/")
def index(request: Request, token: str | None = None):
    global _BOOTSTRAP_TOKEN
    if token and _BOOTSTRAP_TOKEN and secrets.compare_digest(token, _BOOTSTRAP_TOKEN):
        _BOOTSTRAP_TOKEN = None          # 一次性：换成 cookie 后立即作废
        try:
            (data_home() / "console_url.txt").unlink(missing_ok=True)   # 不把已用 token 留在磁盘
        except OSError:
            pass
        resp = RedirectResponse("/")
        resp.set_cookie("console_token", CONSOLE_TOKEN, httponly=True, samesite="strict")
        return resp
    if not secrets.compare_digest(request.cookies.get("console_token") or "", CONSOLE_TOKEN):
        raise HTTPException(401, "用启动时打印的带 token 地址打开")
    return FileResponse(STATIC / "index.html")


@app.get("/app.js")
def app_js():
    return FileResponse(STATIC / "app.js", media_type="application/javascript")


# ---------------------------------------------------------------- 控制台：平台与会话
C = [Depends(require_console)]


class NewSession(BaseModel):
    title: str
    platform: str
    account: str | None = None


class Ask(BaseModel):
    text: str


class OpenPlatform(BaseModel):
    account: str | None = None
    autofill: bool = False


@app.get("/api/platforms", dependencies=C)
def platforms():
    return {"platforms": S.engine.platform_status(), "relay_order": CFG["relay_order"]}


@app.post("/api/platforms/{pid}/open", dependencies=C)
async def open_platform(pid: str, b: OpenPlatform | None = None):
    account = (b.account if b and b.account else "main")
    ad = await S.engine.adapter(pid, account)     # 打开该平台该账号的 Chrome，首次请登录
    # 可选：autofill 开启且凭据库里有这个账号的凭据时，代填登录表单（凭据不进日志）
    if b and b.autofill and CFG["platforms"].get(pid, {}).get("autofill"):
        if await ad.health():
            return {"ok": True, "logged_in": True}
        cred = get_platform_credential(pid, account)
        if cred:
            await ad.fill_login(cred[0], cred[1])
    return {"ok": True}


@app.get("/api/platforms/{pid}/chats", dependencies=C)
async def platform_chats(pid: str):
    ad = await S.engine.adapter(pid)
    return {"chats": await ad.list_chats()}


class ResumeChat(BaseModel):
    platform: str
    url: str
    title: str = ""
    account: str | None = None


@app.post("/api/sessions/resume", dependencies=C)
def resume_session(b: ResumeChat):
    """把平台侧的一个历史会话接进控制台：记录 chat_ref，之后提问会回到该会话。"""
    if b.platform not in adapters.available():
        raise HTTPException(400, "unknown platform")
    account = b.account or (platform_accounts(CFG, b.platform) or ["main"])[0]
    sid = S.main.create_session(b.title or f"{b.platform} 历史会话", b.platform, account=account)
    S.main.set_chat_ref(sid, b.url)
    return {"id": sid}


@app.get("/api/sessions", dependencies=C)
def sessions():
    return S.main.list_sessions()


@app.post("/api/sessions", dependencies=C)
def new_session(b: NewSession):
    if b.platform not in adapters.available():
        raise HTTPException(400, "unknown platform")
    accounts = platform_accounts(CFG, b.platform)
    account = b.account if b.account in accounts else accounts[0]
    return {"id": S.main.create_session(b.title, b.platform, account=account)}


@app.get("/api/sessions/{sid}", dependencies=C)
def session_detail(sid: str):
    s = S.main.get_session(sid)
    linked = [i["id"] for i in S.arena.list_items() if i.get("session_id") == sid]
    return {"id": s.id, "title": s.title, "platform": s.current_platform, "account": s.account,
            "chain": s.chain, "summary": s.summary, "messages": S.main.messages(sid),
            "handoffs": S.main.handoffs(sid), "pool_item_id": linked[0] if linked else None}


@app.post("/api/sessions/{sid}/ask", dependencies=C)
async def ask(sid: str, b: Ask):
    return await S.engine.ask(sid, b.text)


# ---------------------------------------------------------------- 控制台：对抗池管理
class Publish(BaseModel):
    title: str
    session_id: str | None = None
    mode: str = "text"            # text | summary | messages
    text: str | None = None       # mode=text 时为你编辑/脱敏后的内容
    last_n: int = 6
    audience: str = "*"           # "*" 或逗号分隔的 participant_id


@app.post("/api/pool/publish", dependencies=C)
def publish(b: Publish):
    if b.mode == "text":
        content = b.text or ""
    else:
        if not b.session_id:
            raise HTTPException(400, "该发布模式需要选择源会话")
        s = S.main.get_session(b.session_id)
        if b.mode == "summary":
            content = s.summary or ""
        else:
            msgs = S.main.messages(b.session_id, limit=b.last_n)
            content = "\n".join(f"{m['role']}: {m['content']}" for m in msgs)
    if not content.strip():
        raise HTTPException(400, "内容为空")
    # 会话池 ↔ Agent 读取池一一对应：带 session_id 的发布总是更新同一条目
    iid = S.arena.publish_item(b.title, content, b.audience, session_id=b.session_id)
    return {"id": iid, "linked": bool(b.session_id)}


@app.get("/api/pool/items", dependencies=C)
def pool_items():
    return S.arena.list_items()


@app.delete("/api/pool/items/{iid}", dependencies=C)
def pool_delete(iid: str):
    S.arena.delete_item(iid)
    return {"ok": True}


class NewAgent(BaseModel):
    name: str


@app.get("/api/agents", dependencies=C)
def agents():
    return S.arena.list_agents()


@app.post("/api/agents", dependencies=C)
def new_agent(b: NewAgent):
    pid, token = S.arena.create_agent(b.name)
    return {"participant_id": pid, "token": token, "note": "令牌只显示这一次，填进该 Agent 的 MCP 配置"}


@app.post("/api/agents/{pid}/revoke", dependencies=C)
def revoke_agent(pid: str):
    S.arena.revoke_agent(pid)
    return {"ok": True}


@app.get("/api/approvals", dependencies=C)
def approvals():
    return {"pending": S.arena.pending_requests(), "allow_batch": CFG["arena"]["allow_batch_approve"]}


@app.post("/api/approvals/{rid}/{decision}", dependencies=C)
def decide(rid: str, decision: str):
    if decision not in ("approve", "deny"):
        raise HTTPException(400)
    S.arena.decide(rid, decision == "approve")
    return {"ok": True}


@app.post("/api/arenas/{aid}/approve-round", dependencies=C)
def approve_round(aid: str):
    if not CFG["arena"]["allow_batch_approve"]:
        raise HTTPException(403, "未启用批量批准")
    return {"approved": S.arena.approve_round(aid)}


@app.get("/api/audit", dependencies=C)
def audit():
    return S.arena.audit_log()


# ---------------------------------------------------------------- 控制台：对抗
class NewArena(BaseModel):
    mode: str
    topic: str
    rules: str = ""
    seats: list[dict]


@app.get("/api/arenas", dependencies=C)
def arenas():
    return S.arena.list_arenas()


@app.post("/api/arenas", dependencies=C)
def new_arena(b: NewArena):
    return {"id": S.runner.create(b.mode, b.topic, b.rules, b.seats)}


@app.post("/api/arenas/{aid}/round", dependencies=C)
async def run_round(aid: str):
    t = S.round_tasks.get(aid)
    if t and not t.done():
        raise HTTPException(409, "本轮仍在进行")
    task = asyncio.create_task(S.runner.run_round(aid))
    S.round_tasks[aid] = task

    def _done(t: asyncio.Task) -> None:
        if not t.cancelled():
            t.exception()      # 取回异常，避免 "never retrieved"；状态由 arena_detail 读取

    task.add_done_callback(_done)
    return {"started": True}


def _round_status(t: asyncio.Task | None) -> str:
    if t is None:
        return "idle"
    if not t.done():
        return "running"
    if t.cancelled():
        return "cancelled"
    err = t.exception()
    return f"error: {err}" if err else "done"


@app.get("/api/arenas/{aid}", dependencies=C)
def arena_detail(aid: str):
    t = S.round_tasks.get(aid)
    return S.arena.arena(aid) | {"turns": S.arena.turns(aid), "round_status": _round_status(t)}


# ---------------------------------------------------------------- 控制台：设置与账号登记
class SettingsUpdate(BaseModel):
    relay_order: list[str] | None = None
    default_cooldown_minutes: int | None = None
    relay_on_truncation: bool | None = None
    near_threshold_chars: int | None = None
    every_n_turns: int | None = None
    recent_rounds_raw: int | None = None
    agent_turn_timeout: int | None = None
    allow_batch_approve: bool | None = None


@app.get("/api/settings", dependencies=C)
def get_settings():
    return {"host": CFG["host"], "port": CFG["port"], "data_dir": CFG["data_dir"],
            "relay_order": CFG["relay_order"],
            "default_cooldown_minutes": CFG.get("default_cooldown_minutes", 180),
            "relay_on_truncation": CFG.get("relay_on_truncation", False),
            "near_threshold_chars": CFG["summary"].get("near_threshold_chars", 0),
            "every_n_turns": CFG["summary"].get("every_n_turns", 4),
            "recent_rounds_raw": CFG["summary"].get("recent_rounds_raw", 3),
            "agent_turn_timeout": CFG["arena"].get("agent_turn_timeout", 600),
            "allow_batch_approve": CFG["arena"].get("allow_batch_approve", False)}


@app.post("/api/settings", dependencies=C)
def update_settings(b: SettingsUpdate):
    """基本管理：运行时改 CFG 并写回 config.yaml（整体落盘，注释会丢失）。"""
    if b.relay_order is not None:
        order = [x.strip() for x in b.relay_order if x.strip()]
        unknown = [x for x in order if x not in adapters.available()]
        if unknown:
            raise HTTPException(400, f"未知平台: {unknown}")
        CFG["relay_order"] = order
    if b.default_cooldown_minutes is not None:
        CFG["default_cooldown_minutes"] = max(1, int(b.default_cooldown_minutes))
    if b.relay_on_truncation is not None:
        CFG["relay_on_truncation"] = bool(b.relay_on_truncation)
    if b.near_threshold_chars is not None:
        CFG["summary"]["near_threshold_chars"] = max(0, int(b.near_threshold_chars))
    if b.every_n_turns is not None:
        CFG["summary"]["every_n_turns"] = max(1, int(b.every_n_turns))
    if b.recent_rounds_raw is not None:
        CFG["summary"]["recent_rounds_raw"] = max(0, int(b.recent_rounds_raw))
    if b.agent_turn_timeout is not None:
        CFG["arena"]["agent_turn_timeout"] = max(30, int(b.agent_turn_timeout))
    if b.allow_batch_approve is not None:
        CFG["arena"]["allow_batch_approve"] = bool(b.allow_batch_approve)
    save_config(CFG)
    return get_settings()


class AccountsUpdate(BaseModel):
    accounts: list[str] | None = None       # 全量替换该平台的账号列表
    autofill: bool | None = None


@app.get("/api/platforms/{pid}/accounts", dependencies=C)
def get_accounts(pid: str):
    if pid not in adapters.available():
        raise HTTPException(400, "unknown platform")
    return {"platform": pid, "accounts": platform_accounts(CFG, pid),
            "autofill": bool(CFG["platforms"].get(pid, {}).get("autofill"))}


@app.post("/api/platforms/{pid}/accounts", dependencies=C)
def update_accounts(pid: str, b: AccountsUpdate):
    """账户页登记：维护平台的多账号池与 autofill 开关，立即生效并持久化。"""
    if pid not in adapters.available():
        raise HTTPException(400, "unknown platform")
    if b.accounts is not None:
        names = [x.strip() for x in b.accounts if x and x.strip()]
        if not names:
            raise HTTPException(400, "至少保留一个账号")
        if len(names) != len(set(names)):
            raise HTTPException(400, "账号名不能重复")
        CFG["platforms"].setdefault(pid, {})["accounts"] = names
    if b.autofill is not None:
        CFG["platforms"].setdefault(pid, {})["autofill"] = bool(b.autofill)
    save_config(CFG)
    return get_accounts(pid)


# ---------------------------------------------------------------- Agent 接口（MCP 只调这些）
class ReadReq(BaseModel):
    scope_type: str               # items | turn | session
    item_ids: list[str] = []
    arena_id: str | None = None
    session_id: str | None = None
    reason: str


class Submit(BaseModel):
    content: str


@app.get("/agent/pool")
def agent_pool(pid: str = Depends(require_agent)):
    S.arena.audit(pid, "list_pool")
    return [{"id": i["id"], "title": i["title"], "published_at": i["published_at"]} for i in S.arena.list_items(pid)]


@app.post("/agent/requests")
def agent_request(b: ReadReq, pid: str = Depends(require_agent)):
    # 不在申请阶段校验会话是否存在：否则 Agent 能靠 400/200 枚举会话 ID。
    # 存在性留到 fetch 阶段（那时已获批准）再判断。
    scope = {"item_ids": b.item_ids} if b.scope_type == "items" else \
        {"arena_id": b.arena_id} if b.scope_type == "turn" else {"session_id": b.session_id}
    return {"request_id": S.arena.create_request(pid, b.scope_type, scope, b.reason), "status": "pending"}


@app.get("/agent/requests/{rid}")
def agent_check(rid: str, pid: str = Depends(require_agent)):
    return {"request_id": rid, "status": S.arena.check_request(pid, rid)}


@app.post("/agent/requests/{rid}/fetch")
def agent_fetch(rid: str, pid: str = Depends(require_agent)):
    data = S.arena.fetch(pid, rid)
    if isinstance(data, dict) and data.get("scope_type") == "session":
        data |= _session_status_payload(data["session_id"])
        S.arena.audit(pid, "session_status", data["session_id"])
    return {"request_id": rid, "data": data}


def _session_status_payload(sid: str) -> dict:
    """会话状态（只读、经批准）：接力链、各平台账号冷却、摘要预览。

    摘要由对话正文生成，可能包含要点；这里只给前 600 字，不是“绝不包含任何正文”。
    """
    try:
        s = S.main.get_session(sid)
    except KeyError as exc:
        raise PoolError("NOT_FOUND", sid) from exc
    return {"title": s.title, "platform": s.current_platform, "account": s.account,
            "chain": s.chain, "platforms": S.engine.platform_status(),
            "summary_preview": (s.summary or "")[:600]}


@app.post("/agent/arenas/{aid}/submit")
def agent_submit(aid: str, b: Submit, pid: str = Depends(require_agent)):
    S.arena.submit_turn(pid, aid, b.content)
    return {"ok": True}
