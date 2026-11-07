"""控制台后端。

两类接口，两种身份：
  /api/*    控制台（你）  —— 需要启动时生成的控制台令牌（cookie 或 X-Console-Token）
  /agent/*  Agent（经 MCP）—— 需要 Agent 令牌（Bearer），且只能碰对抗池；读取一律需你批准
"""
from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import adapters
from adapters.spec import AdapterError

from .arena import ArenaRunner
from .browser import ChromeManager
from .config import load_config
from .crypto import CRED_ARENA, CRED_MAIN, get_api_key, get_fernet
from .relay_engine import RelayEngine
from .storage import ArenaPool, MainPool, PoolError
from .summarizer import Summarizer

CFG = load_config()
CONSOLE_TOKEN = secrets.token_urlsafe(24)
STATIC = Path(__file__).with_name("static")


class State:
    main: MainPool
    arena: ArenaPool
    browser: ChromeManager
    engine: RelayEngine
    runner: ArenaRunner
    round_tasks: dict[str, asyncio.Task] = {}


S = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.main = MainPool(CFG["data_dir"], get_fernet(CRED_MAIN))
    S.arena = ArenaPool(CFG["data_dir"], get_fernet(CRED_ARENA), CFG["approvals"]["ticket_ttl_seconds"])
    S.browser = ChromeManager(CFG)
    S.engine = RelayEngine(CFG, S.main, S.browser, Summarizer(CFG, S.main, get_api_key()))
    S.runner = ArenaRunner(CFG, S.main, S.arena, S.engine)
    print(f"\n  控制台地址: http://{CFG['host']}:{CFG['port']}/?token={CONSOLE_TOKEN}\n")
    yield
    await S.browser.close()


app = FastAPI(title="Relay Console", lifespan=lifespan)


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
    if token and secrets.compare_digest(token, CONSOLE_TOKEN):
        resp = RedirectResponse("/")
        resp.set_cookie("console_token", token, httponly=True, samesite="strict")
        return resp
    if not secrets.compare_digest(request.cookies.get("console_token") or "", CONSOLE_TOKEN):
        raise HTTPException(401, "用启动时打印的带 token 地址打开")
    return FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------- 控制台：平台与会话
C = [Depends(require_console)]


class NewSession(BaseModel):
    title: str
    platform: str


class Ask(BaseModel):
    text: str


@app.get("/api/platforms", dependencies=C)
def platforms():
    return {"platforms": S.engine.platform_status(), "relay_order": CFG["relay_order"]}


@app.post("/api/platforms/{pid}/open", dependencies=C)
async def open_platform(pid: str):
    await S.engine.adapter(pid)          # 打开该平台的 Chrome，首次请手动登录
    return {"ok": True}


@app.get("/api/sessions", dependencies=C)
def sessions():
    return S.main.list_sessions()


@app.post("/api/sessions", dependencies=C)
def new_session(b: NewSession):
    if b.platform not in adapters.available():
        raise HTTPException(400, "unknown platform")
    return {"id": S.main.create_session(b.title, b.platform)}


@app.get("/api/sessions/{sid}", dependencies=C)
def session_detail(sid: str):
    s = S.main.get_session(sid)
    return {"id": s.id, "title": s.title, "platform": s.current_platform, "chain": s.chain,
            "summary": s.summary, "messages": S.main.messages(sid), "handoffs": S.main.handoffs(sid)}


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
        s = S.main.get_session(b.session_id)
        if b.mode == "summary":
            content = s.summary or ""
        else:
            msgs = S.main.messages(b.session_id, limit=b.last_n)
            content = "\n".join(f"{m['role']}: {m['content']}" for m in msgs)
    if not content.strip():
        raise HTTPException(400, "内容为空")
    return {"id": S.arena.publish_item(b.title, content, b.audience)}


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
    S.round_tasks[aid] = asyncio.create_task(S.runner.run_round(aid))
    return {"started": True}


@app.get("/api/arenas/{aid}", dependencies=C)
def arena_detail(aid: str):
    t = S.round_tasks.get(aid)
    status = "idle" if not t else ("running" if not t.done() else
                                   ("error: " + str(t.exception())) if t.exception() else "done")
    return S.arena.arena(aid) | {"turns": S.arena.turns(aid), "round_status": status}


# ---------------------------------------------------------------- Agent 接口（MCP 只调这些）
class ReadReq(BaseModel):
    scope_type: str               # items | turn
    item_ids: list[str] = []
    arena_id: str | None = None
    reason: str


class Submit(BaseModel):
    content: str


@app.get("/agent/pool")
def agent_pool(pid: str = Depends(require_agent)):
    S.arena.audit(pid, "list_pool")
    return [{"id": i["id"], "title": i["title"], "published_at": i["published_at"]} for i in S.arena.list_items(pid)]


@app.post("/agent/requests")
def agent_request(b: ReadReq, pid: str = Depends(require_agent)):
    scope = {"item_ids": b.item_ids} if b.scope_type == "items" else {"arena_id": b.arena_id}
    return {"request_id": S.arena.create_request(pid, b.scope_type, scope, b.reason), "status": "pending"}


@app.get("/agent/requests/{rid}")
def agent_check(rid: str, pid: str = Depends(require_agent)):
    return {"request_id": rid, "status": S.arena.check_request(pid, rid)}


@app.post("/agent/requests/{rid}/fetch")
def agent_fetch(rid: str, pid: str = Depends(require_agent)):
    return {"request_id": rid, "data": S.arena.fetch(pid, rid)}


@app.post("/agent/arenas/{aid}/submit")
def agent_submit(aid: str, b: Submit, pid: str = Depends(require_agent)):
    S.arena.submit_turn(pid, aid, b.content)
    return {"ok": True}
