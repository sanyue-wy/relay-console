"""滚动摘要 + 交接包。

每 N 轮增量更新一次摘要并加密存入主池；交接时直接取缓存，API 不可用也能交接。
只读取「当前会话自己」的消息，对抗模式下不会混入对手的私有内容。

摘要提供者（A1）：所有提供者都按 OpenAI 兼容接口调用；预设只用来填默认值，
用户仍可在设置页修改 base_url / model。密钥只存系统凭据库，不进配置与日志。
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

from openai import AsyncOpenAI

from .storage import MainPool

log = logging.getLogger("relay.summarizer")

# 摘要提供者预设：只用于填默认值；custom 需要用户自己填 base_url 与 model
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "custom": {"base_url": "", "model": ""},
}

SUMMARY_PROMPT = """你是对话交接助手。请把【已有摘要】和【新增对话】合并成一份新的结构化交接摘要，用中文，严格使用以下四个小节：
## 目标与约束
## 已确认的事实与结论
## 当前进度
## 下一步与待决问题
要求：保留具体数字、代码名、文件名、决定；删掉寒暄；不超过 1200 字。"""

HANDOFF_HEADER = ("【会话接力】下面是我和另一个 AI 助手此前对话的交接摘要和最近几轮原文。"
                  "请直接在此基础上继续回答，不要复述摘要，也不要说明你读过它。\n\n")

# 手动切换不带问题时，在交接包末尾追加的确认请求（A3）
CONFIRM_QUESTION = "请用一句话确认你已了解以上背景，等待我的下一个问题"


def validate_base_url(url: str) -> str | None:
    """校验 base_url：只允许 https；本机地址 http://127.0.0.1 与 http://localhost 例外
    （供 Ollama 等本地模型使用）。返回 None 表示通过，否则返回中文错误。"""
    u = (url or "").strip()
    if not u:
        return "base_url 不能为空"
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return "base_url 必须是 http(s) 地址"
    if p.scheme == "http" and p.hostname not in ("127.0.0.1", "localhost", "::1"):
        return "base_url 只允许 https（本机地址 127.0.0.1 / localhost 可用 http，供本地模型使用）"
    return None


def friendly_api_error(e: Exception) -> str:
    """把 openai SDK 的异常翻译成面向用户的中文提示。"""
    import openai
    if isinstance(e, openai.AuthenticationError):
        return "密钥无效（401）：请检查 API 密钥是否正确"
    if isinstance(e, openai.NotFoundError):
        return "模型或地址错误（404）：请检查模型名与 base_url 是否正确"
    if isinstance(e, openai.PermissionDeniedError):
        return "无权限（403）：该密钥可能未开通此模型"
    if isinstance(e, openai.RateLimitError):
        return "请求被限流（429）：请稍后重试或检查额度"
    if isinstance(e, openai.APITimeoutError):
        return "网络超时：无法在限时内连通提供者，请检查网络或调大超时"
    if isinstance(e, openai.APIConnectionError):
        return "网络连接失败：请检查 base_url 与本机网络"
    if isinstance(e, openai.BadRequestError):
        return f"请求被拒绝（400）：{e}"
    return f"调用失败：{e}"


class Summarizer:
    def __init__(self, cfg, main: MainPool, api_key: str | None = None):
        self.cfg = cfg["summary"]
        self.main = main
        self.client: AsyncOpenAI | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._apply_client(api_key)

    # ---------------- 客户端管理（A1：可热更新）
    def _apply_client(self, api_key: str | None) -> None:
        """按当前 cfg 与密钥重建客户端；没有密钥时 client 为 None，行为与未配置一致。"""
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.cfg.get("base_url") or None,
            timeout=float(self.cfg.get("timeout_seconds", 25)),
            max_retries=0,
        ) if api_key else None

    @property
    def available(self) -> bool:
        """是否已配置摘要提供者（有客户端）。"""
        return self.client is not None

    async def reconfigure(self, base_url: str, model: str, api_key: str | None) -> None:
        """热更新：更新配置、重建客户端并关闭旧客户端。api_key 为空时退回未配置状态。"""
        self.cfg["base_url"] = base_url
        self.cfg["model"] = model
        old = self.client
        self._apply_client(api_key)
        if old is not None:
            try:
                await old.close()
            except Exception:      # 关闭旧连接失败不影响新客户端
                pass

    async def test_connection(self, base_url: str, model: str, api_key: str,
                              timeout: float = 20.0) -> dict:
        """用临时配置发一次极短请求（max_tokens ≤ 8），返回 {ok, latency_ms, model, error}。
        临时客户端用完即关，不影响运行中的客户端。"""
        t0 = time.perf_counter()
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
        try:
            await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "请只回复一个字：好"}],
                max_tokens=8,
                temperature=0,
            )
            return {"ok": True, "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "model": model, "error": None}
        except Exception as e:
            return {"ok": False, "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "model": model, "error": friendly_api_error(e)}
        finally:
            try:
                await client.close()
            except Exception:
                pass

    def _lock(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    async def update(self, sid: str) -> str | None:
        if not self.client:
            return None
        async with self._lock(sid):
            s = self.main.get_session(sid)
            new = self.main.messages(sid, after_id=s.summarized_upto)
            if not new:
                return s.summary
            convo = "\n".join(f"{'用户' if m['role'] == 'user' else '助手'}: {m['content']}" for m in new)
            resp = await asyncio.wait_for(self.client.chat.completions.create(
                model=self.cfg["model"],
                messages=[{"role": "system", "content": SUMMARY_PROMPT},
                          {"role": "user", "content": f"【已有摘要】\n{s.summary or '（无）'}\n\n【新增对话】\n{convo}"}],
                temperature=0.2,
            ), timeout=self.cfg.get("timeout_seconds", 25))
            text = resp.choices[0].message.content.strip()
            self.main.set_summary(sid, text, new[-1]["id"])
            return text

    async def maybe_update(self, sid: str) -> None:
        s = self.main.get_session(sid)
        pending = self.main.messages(sid, after_id=s.summarized_upto)
        turns = len([m for m in pending if m["role"] == "assistant"])
        # 主动阈值：网页版看不到额度百分比，用未摘要部分的字符量近似估计——
        # 超过阈值就提前生成摘要并缓存，切换时不必等 API（雏形里的 85% 预生成）。
        near = bool(self.cfg.get("near_threshold_chars")) and \
            sum(len(m["content"]) for m in pending) >= self.cfg["near_threshold_chars"]
        if turns >= self.cfg.get("every_n_turns", 4) or near:
            try:
                await self.update(sid)
            except Exception as e:  # 摘要失败不影响对话
                log.warning("summary update failed: %s", e)

    def assemble_package(self, sid: str, recent_rounds: int | None = None) -> str:
        """由当前缓存摘要 + 最近几轮原文拼出交接包（不调 API）。

        handoff_package（自动接力/手动切换）与 handoff/preview（A2）共用此拼装逻辑，
        保证预览和实际投递的内容一致。
        """
        s = self.main.get_session(sid)
        rounds = self.cfg.get("recent_rounds_raw", 3) if recent_rounds is None else max(0, int(recent_rounds))
        recent = self.main.messages(sid, limit=rounds * 2)
        # Ghost messages：最近几轮以引用块回放，让新会话“看起来没有中断”
        ghost = "\n\n".join(
            f"> **{'用户' if m['role'] == 'user' else 'AI（' + (m.get('platform') or '') + '）'}**："
            f"{m['content'][:800]}" for m in recent)
        summary = s.summary or "（暂无摘要，以下为最近对话原文）"
        return f"{HANDOFF_HEADER}{summary}\n\n## 最近的对话（原文回放）\n{ghost}"

    async def handoff_package(self, sid: str, recent_rounds: int | None = None) -> str:
        try:
            await self.update(sid)          # 尽量补齐最新几轮
        except Exception as e:
            log.warning("handoff: use cached summary (%s)", e)
        return self.assemble_package(sid, recent_rounds)
