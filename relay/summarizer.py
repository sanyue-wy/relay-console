"""滚动摘要 + 交接包。

每 N 轮增量更新一次摘要并加密存入主池；交接时直接取缓存，API 不可用也能交接。
只读取「当前会话自己」的消息，对抗模式下不会混入对手的私有内容。
"""
from __future__ import annotations

import asyncio
import logging

from openai import AsyncOpenAI

from .storage import MainPool

log = logging.getLogger("relay.summarizer")

SUMMARY_PROMPT = """你是对话交接助手。请把【已有摘要】和【新增对话】合并成一份新的结构化交接摘要，用中文，严格使用以下四个小节：
## 目标与约束
## 已确认的事实与结论
## 当前进度
## 下一步与待决问题
要求：保留具体数字、代码名、文件名、决定；删掉寒暄；不超过 1200 字。"""

HANDOFF_HEADER = ("【会话接力】下面是我和另一个 AI 助手此前对话的交接摘要和最近几轮原文。"
                  "请直接在此基础上继续回答，不要复述摘要，也不要说明你读过它。\n\n")


class Summarizer:
    def __init__(self, cfg, main: MainPool, api_key: str | None):
        self.cfg = cfg["summary"]
        self.main = main
        self.client = AsyncOpenAI(api_key=api_key, base_url=self.cfg["base_url"]) if api_key else None
        self._locks: dict[str, asyncio.Lock] = {}

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

    async def handoff_package(self, sid: str) -> str:
        try:
            await self.update(sid)          # 尽量补齐最新几轮
        except Exception as e:
            log.warning("handoff: use cached summary (%s)", e)
        s = self.main.get_session(sid)
        n = self.cfg.get("recent_rounds_raw", 3) * 2
        recent = self.main.messages(sid, limit=n)
        raw = "\n".join(f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:2000]}" for m in recent)
        summary = s.summary or "（暂无摘要，以下为最近对话原文）"
        return f"{HANDOFF_HEADER}{summary}\n\n## 最近对话原文\n{raw}"
