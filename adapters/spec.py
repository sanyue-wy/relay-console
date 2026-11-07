"""参与者适配器规格 v1.0

所有对话方（网页平台、Agent）都实现 Participant。
新增平台 = 新建一个类 + 一段 selectors.yaml + @register，不改核心代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, Flag, auto
from typing import AsyncIterator, Callable, Literal

SPEC_VERSION = "1.0"


# ---------- 基础类型 ----------
class Kind(str, Enum):
    WEB = "web"        # 网页平台，经浏览器驱动
    AGENT = "agent"    # 外部 Agent，经 MCP + 对抗池交互


class Cap(Flag):
    """能力声明，核心按能力降级，不按平台名写 if。"""
    NEW_CHAT = auto()
    STREAM = auto()
    FILE_UPLOAD = auto()
    LIMIT_DETECT = auto()
    RESUME_URL = auto()     # 能用 URL 回到旧会话
    PREAMBLE = auto()       # 支持自定义指令/预设（否则交接包作首条消息）


class LimitStatus(str, Enum):
    OK = "ok"
    NEAR = "near"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LimitState:
    status: LimitStatus = LimitStatus.UNKNOWN
    reset_at: datetime | None = None
    signal: Literal["dom", "network", "behavior", "none"] = "none"
    detail: str = ""


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant", "moderator"]
    content: str
    author: str                      # participant_id 或 "moderator"
    ts: datetime = field(default_factory=datetime.now)
    attachments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reply:
    text: str
    finished: bool                   # False = 被截断/超时
    latency_ms: int
    chat_ref: str | None = None      # 平台会话 URL 或 ID


@dataclass(frozen=True)
class AdapterInfo:
    id: str                          # "deepseek" / "kimi" / "agent:claude-code"
    display_name: str
    kind: Kind
    caps: Cap
    spec_version: str = SPEC_VERSION
    max_input_chars: int | None = None   # 超过则走 FILE_UPLOAD 或分段


# ---------- 错误 ----------
class AdapterError(Exception):
    code = "ADAPTER_ERROR"


class NotReady(AdapterError):
    code = "NOT_READY"               # 未登录 / 页面未加载 / Agent 未连接


class LimitReached(AdapterError):
    code = "LIMIT_REACHED"

    def __init__(self, state: LimitState):
        super().__init__(state.detail)
        self.state = state


class SelectorBroken(AdapterError):
    code = "SELECTOR_BROKEN"         # 网页改版，去改 selectors.yaml


class ReplyTimeout(AdapterError):
    code = "REPLY_TIMEOUT"


# ---------- 核心接口 ----------
class Participant(ABC):
    info: AdapterInfo

    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def health(self) -> bool: ...

    @abstractmethod
    async def new_chat(self, preamble: str | None = None) -> str:
        """开新会话，返回 chat_ref。preamble = 交接包/角色设定。"""

    async def resume_chat(self, chat_ref: str) -> None:
        """回到旧会话。声明 Cap.RESUME_URL 的子类实现。"""
        raise NotImplementedError

    @abstractmethod
    async def send(self, turn: Turn) -> None: ...

    @abstractmethod
    async def receive(self, timeout: float = 180) -> Reply: ...

    async def stream(self, timeout: float = 180) -> AsyncIterator[str]:
        """默认非流式：一次吐出完整回复。支持 Cap.STREAM 的子类覆盖。"""
        yield (await self.receive(timeout)).text

    async def limit_state(self) -> LimitState:
        return LimitState()


# ---------- 网页平台 ----------
@dataclass(frozen=True)
class SelectorSet:
    """来自 selectors.yaml，网页改版只改这里。"""
    url: str
    input: str
    send_button: str | None
    stop_button: str | None          # 存在 = 仍在生成
    last_reply: str
    new_chat: str | None = None
    upload_input: str | None = None
    limit_texts: tuple[str, ...] = ()
    limit_status_codes: tuple[int, ...] = (429,)


class WebAdapter(Participant):
    """通用实现走 SelectorSet；平台特殊行为在子类覆盖对应方法。"""

    def __init__(self, info: AdapterInfo, selectors: SelectorSet, page_provider: Callable):
        self.info, self.sel, self._page_provider = info, selectors, page_provider

    async def upload_file(self, path: str) -> bool:
        raise NotImplementedError

    # 子类可覆盖的钩子
    async def _wait_generation_done(self, timeout: float) -> None: ...
    async def _extract_last_reply(self) -> str: ...


# ---------- Agent ----------
# 具体实现见 adapters/agent.py 的 AgentParticipant（实现 Participant，经对抗池收发）。


# ---------- 注册表 ----------
_REGISTRY: dict[str, type[Participant]] = {}


def register(adapter_id: str):
    def deco(cls: type[Participant]):
        if adapter_id in _REGISTRY:
            raise ValueError(f"duplicate adapter id: {adapter_id}")
        _REGISTRY[adapter_id] = cls
        return cls
    return deco


def create(adapter_id: str, **config) -> Participant:
    return _REGISTRY[adapter_id](**config)


def available() -> list[str]:
    return sorted(_REGISTRY)
